"""Deterministic fixed-offset logistic models for M5 prematch predictions."""

from __future__ import annotations

import hashlib
import hmac
import math
import platform
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from numbers import Real
from typing import Any, Iterable, Mapping

import numpy as np
import scipy
from scipy.optimize import minimize
from scipy.special import expit

from .draft_features import AvailabilityMode
from .draft_residual_features import DRAFT_RESIDUAL_MODEL_SCHEMA
from .prematch_features import (
    PREMATCH_CLUSTER_MODEL_SCHEMA,
    PREMATCH_FEATURE_VERSION,
    PrematchFeatureSnapshot,
    prematch_feature_schema,
    prematch_feature_schema_hash,
    project_prematch_features,
    verify_prematch_feature_snapshot,
)
from .raw_archive import canonical_json_bytes
from .rosh_features import ROSH_MODEL_SCHEMA


UTC = timezone.utc
PREMATCH_MODEL_VERSION = "prematch-offset-logistic-l2-v2"
PREMATCH_MODEL_ARTIFACT_VERSION = "prematch-model-artifact-v1"
DEFAULT_MIN_SAMPLES = 20
DEFAULT_L2_REGULARIZATION = 1.0
PREMATCH_MIN_FEATURE_NONMISSING_SUPPORT = 20
PREMATCH_MIN_STANDARDIZATION_SCALE = 1e-6
PREMATCH_MAX_ABS_STANDARDIZED_VALUE = 8.0
PREMATCH_SOLVER_METHOD = "L-BFGS-B"
PREMATCH_MAX_ITERATIONS = 2_000
PREMATCH_FTOL = 1e-12
PREMATCH_GTOL = 1e-8
PREMATCH_MAX_LINE_SEARCH_STEPS = 50
PREMATCH_PINV_RCOND = 1e-12
PREMATCH_TRAINER_RUNTIME = (
    ("numpy", np.__version__),
    ("python_implementation", platform.python_implementation()),
    ("python_version", platform.python_version()),
    ("scipy", scipy.__version__),
)

FeatureValue = float | int | None


class ModelStatus(str, Enum):
    TRAINED = "trained"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class PredictionStatus(str, Enum):
    PREDICTED = "predicted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


def _hash(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _utc(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _binary(value: object, field: str = "outcome") -> int:
    if isinstance(value, (bool, np.bool_)):
        return int(value)
    if isinstance(value, Real) and not isinstance(value, bool):
        result = float(value)
        if math.isfinite(result) and result in (0.0, 1.0):
            return int(result)
    raise ValueError(f"{field} must be 0 or 1")


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value


def _mode(value: object) -> str:
    try:
        return AvailabilityMode(value).value
    except (TypeError, ValueError) as error:
        raise ValueError("unsupported prematch availability mode") from error


@dataclass(frozen=True)
class PrematchTrainingRow:
    """One settled historical prediction offered to a training invocation."""

    match_id: int
    input_snapshot_hash: str
    prediction_cutoff: datetime
    completed_at: datetime
    result_usable_at: datetime | None
    availability_mode: str
    outcome: int | bool
    series_id: str | int
    event_id: str
    patch_id: str
    team_base_logit: float
    features: Mapping[str, FeatureValue]

    @classmethod
    def from_snapshot(
        cls,
        snapshot: PrematchFeatureSnapshot,
        *,
        model_kind: str,
        completed_at: datetime,
        result_usable_at: datetime | None,
        outcome: int | bool,
        series_id: str | int,
        event_id: str,
        patch_id: str,
    ) -> PrematchTrainingRow:
        verify_prematch_feature_snapshot(snapshot)
        return cls(
            match_id=snapshot.match_id,
            input_snapshot_hash=snapshot.input_hash,
            prediction_cutoff=snapshot.prediction_cutoff,
            completed_at=completed_at,
            result_usable_at=result_usable_at,
            availability_mode=snapshot.availability_mode,
            outcome=outcome,
            series_id=series_id,
            event_id=event_id,
            patch_id=patch_id,
            team_base_logit=snapshot.team_base_logit,
            features=project_prematch_features(snapshot, model_kind),
        )


@dataclass(frozen=True)
class PrematchTrainingCorpusRow:
    """Canonical authoritative row embedded in an M5 model artifact."""

    match_id: int
    input_snapshot_hash: str
    prediction_cutoff: datetime
    completed_at: datetime
    result_usable_at: datetime
    availability_mode: str
    outcome: int
    series_id: str
    event_id: str
    patch_id: str
    team_base_logit: float
    features: tuple[tuple[str, float | None], ...]
    missing_features: tuple[str, ...]

    def to_training_row(self) -> PrematchTrainingRow:
        return PrematchTrainingRow(
            match_id=self.match_id,
            input_snapshot_hash=self.input_snapshot_hash,
            prediction_cutoff=self.prediction_cutoff,
            completed_at=self.completed_at,
            result_usable_at=self.result_usable_at,
            availability_mode=self.availability_mode,
            outcome=self.outcome,
            series_id=self.series_id,
            event_id=self.event_id,
            patch_id=self.patch_id,
            team_base_logit=self.team_base_logit,
            features=dict(self.features),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "input_snapshot_hash": self.input_snapshot_hash,
            "prediction_cutoff": self.prediction_cutoff.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "result_usable_at": self.result_usable_at.isoformat(),
            "availability_mode": self.availability_mode,
            "outcome": self.outcome,
            "series_id": self.series_id,
            "event_id": self.event_id,
            "patch_id": self.patch_id,
            "team_base_logit": self.team_base_logit,
            "features": dict(self.features),
            "missing_features": list(self.missing_features),
        }


@dataclass(frozen=True)
class PrematchModelArtifact:
    artifact_version: str
    model_version: str
    feature_version: str
    model_kind: str
    status: ModelStatus
    reason: str | None
    availability_mode: str
    training_cutoff: datetime
    support: int
    series_support: int
    event_support: int
    patch_support: int
    min_samples: int
    l2_regularization: float
    feature_names: tuple[str, ...]
    feature_schema_hash: str
    training_input_hash: str
    trainer_runtime: tuple[tuple[str, str], ...]
    training_corpus: tuple[PrematchTrainingCorpusRow, ...]
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
            "artifact_version": self.artifact_version,
            "model_version": self.model_version,
            "feature_version": self.feature_version,
            "model_kind": self.model_kind,
            "status": self.status.value,
            "reason": self.reason,
            "availability_mode": self.availability_mode,
            "training_cutoff": self.training_cutoff.isoformat(),
            "support": self.support,
            "series_support": self.series_support,
            "event_support": self.event_support,
            "patch_support": self.patch_support,
            "min_samples": self.min_samples,
            "l2_regularization": self.l2_regularization,
            "solver": {
                "method": PREMATCH_SOLVER_METHOD,
                "max_iterations": PREMATCH_MAX_ITERATIONS,
                "ftol": PREMATCH_FTOL,
                "gtol": PREMATCH_GTOL,
                "max_line_search_steps": PREMATCH_MAX_LINE_SEARCH_STEPS,
                "pinv_rcond": PREMATCH_PINV_RCOND,
            },
            "feature_names": list(self.feature_names),
            "feature_schema_hash": self.feature_schema_hash,
            "training_input_hash": self.training_input_hash,
            "trainer_runtime": dict(self.trainer_runtime),
            "training_corpus": [row.to_payload() for row in self.training_corpus],
            "class_counts": {str(key): value for key, value in self.class_counts},
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
        return canonical_json_bytes(self.to_payload())


@dataclass(frozen=True)
class FeatureContribution:
    feature_name: str
    component: str
    input_value: float
    standardized_value: float
    coefficient: float
    log_odds_contribution: float
    was_imputed: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "feature_name": self.feature_name,
            "component": self.component,
            "input_value": self.input_value,
            "standardized_value": self.standardized_value,
            "coefficient": self.coefficient,
            "log_odds_contribution": self.log_odds_contribution,
            "was_imputed": self.was_imputed,
        }


@dataclass(frozen=True)
class PrematchPrediction:
    status: PredictionStatus
    reason: str | None
    raw_probability: float | None
    parameter_uncertainty: float | None
    team_base_logit: float
    learned_intercept: float | None
    draft_logit_delta: float | None
    rosh_logit_delta: float | None
    cluster_logit_delta: float | None
    total_adjustment: float | None
    support: int
    model_hash: str
    input_snapshot_hash: str
    missing_features: tuple[str, ...]
    top_contributions: tuple[FeatureContribution, ...]
    top_cluster_contributions: tuple[FeatureContribution, ...]

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "status": self.status.value,
            "reason": self.reason,
            "raw_probability": self.raw_probability,
            "parameter_uncertainty": self.parameter_uncertainty,
            "team_base_logit": self.team_base_logit,
            "learned_intercept": self.learned_intercept,
            "draft_logit_delta": self.draft_logit_delta,
            "rosh_logit_delta": self.rosh_logit_delta,
            "cluster_logit_delta": self.cluster_logit_delta,
            "total_adjustment": self.total_adjustment,
            "support": self.support,
            "model_hash": self.model_hash,
            "input_snapshot_hash": self.input_snapshot_hash,
            "missing_features": list(self.missing_features),
            "top_contributions": [row.to_payload() for row in self.top_contributions],
        }
        if self.top_cluster_contributions:
            payload["top_cluster_contributions"] = [
                row.to_payload() for row in self.top_cluster_contributions
            ]
        return payload


PreparedRow = tuple[
    PrematchTrainingRow,
    tuple[float | None, ...],
    tuple[str, ...],
]


def _align_features(
    model_kind: str,
    features: Mapping[str, FeatureValue],
) -> tuple[tuple[float | None, ...], tuple[str, ...]]:
    schema = prematch_feature_schema(model_kind)
    if not isinstance(features, Mapping) or any(
        not isinstance(name, str) for name in features
    ):
        raise ValueError("features must be a mapping with string keys")
    unknown = tuple(sorted(set(features) - set(schema)))
    if unknown:
        raise ValueError(f"features outside fixed schema: {', '.join(unknown)}")
    values: list[float | None] = []
    missing: list[str] = []
    for name in schema:
        raw = features.get(name)
        if name.endswith("__missing"):
            if raw is None:
                raise ValueError(f"missing flag {name} must be present")
            number = _finite(raw, f"feature {name}")
            if number not in (0.0, 1.0):
                raise ValueError(f"missing flag {name} must be binary")
            values.append(number)
        elif raw is None:
            values.append(None)
            missing.append(name)
        else:
            values.append(_finite(raw, f"feature {name}"))
    aligned = dict(zip(schema, values, strict=True))
    for name in schema:
        missing_name = f"{name}__missing"
        if missing_name not in aligned:
            continue
        expected = 1.0 if aligned[name] is None else 0.0
        cluster_evidence_flag = (
            name in PREMATCH_CLUSTER_MODEL_SCHEMA
            and aligned[name] is not None
            and aligned[missing_name] == 1.0
        )
        if aligned[missing_name] != expected and not cluster_evidence_flag:
            raise ValueError(f"missing flag {missing_name} disagrees with {name}")
    return tuple(values), tuple(missing)


def _prepared_payload(row: PreparedRow, model_kind: str) -> dict[str, Any]:
    source, values, missing = row
    schema = prematch_feature_schema(model_kind)
    return {
        "match_id": source.match_id,
        "input_snapshot_hash": source.input_snapshot_hash,
        "prediction_cutoff": source.prediction_cutoff.isoformat(),
        "completed_at": source.completed_at.isoformat(),
        "result_usable_at": source.result_usable_at.isoformat(),
        "availability_mode": source.availability_mode,
        "outcome": int(source.outcome),
        "series_id": str(source.series_id),
        "event_id": source.event_id,
        "patch_id": source.patch_id,
        "team_base_logit": source.team_base_logit,
        "features": dict(zip(schema, values, strict=True)),
        "missing_features": list(missing),
    }


def _corpus_row(row: PreparedRow, model_kind: str) -> PrematchTrainingCorpusRow:
    source, values, missing = row
    return PrematchTrainingCorpusRow(
        match_id=source.match_id,
        input_snapshot_hash=source.input_snapshot_hash,
        prediction_cutoff=source.prediction_cutoff,
        completed_at=source.completed_at,
        result_usable_at=source.result_usable_at,
        availability_mode=source.availability_mode,
        outcome=int(source.outcome),
        series_id=str(source.series_id),
        event_id=source.event_id,
        patch_id=source.patch_id,
        team_base_logit=source.team_base_logit,
        features=tuple(zip(prematch_feature_schema(model_kind), values, strict=True)),
        missing_features=missing,
    )


def _column_statistics(
    prepared: tuple[PreparedRow, ...],
    model_kind: str,
) -> tuple[
    np.ndarray,
    tuple[tuple[str, int], ...],
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
]:
    schema = prematch_feature_schema(model_kind)
    support = len(prepared)
    if not schema:
        return np.empty((support, 0), dtype=np.float64), (), (), (), ()
    if not support:
        return (
            np.empty((0, len(schema)), dtype=np.float64),
            tuple((name, 0) for name in schema),
            tuple((name, 0.0) for name in schema),
            tuple((name, 0.0) for name in schema),
            tuple((name, 1.0) for name in schema),
        )

    matrix = np.empty((support, len(schema)), dtype=np.float64)
    columns = tuple(zip(*(values for _row, values, _missing in prepared), strict=True))
    missing_counts: list[tuple[str, int]] = []
    imputation_values: list[tuple[str, float]] = []
    means: list[tuple[str, float]] = []
    scales: list[tuple[str, float]] = []
    for index, (name, column) in enumerate(zip(schema, columns, strict=True)):
        if name.endswith("__missing"):
            complete = np.asarray(column, dtype=np.float64)
            imputation = 0.0
            mean = 0.0
            scale = 1.0
        else:
            observed = tuple(value for value in column if value is not None)
            imputation = math.fsum(observed) / len(observed) if observed else 0.0
            complete = np.asarray(
                [imputation if value is None else value for value in column],
                dtype=np.float64,
            )
            mean = imputation
            if len(observed) < PREMATCH_MIN_FEATURE_NONMISSING_SUPPORT:
                complete.fill(imputation)
                scale = 1.0
            else:
                observed_values = np.asarray(observed, dtype=np.float64)
                scale = max(
                    float(
                        np.sqrt(np.mean(np.square(observed_values - mean)))
                    ),
                    PREMATCH_MIN_STANDARDIZATION_SCALE,
                )
        matrix[:, index] = np.clip(
            (complete - mean) / scale,
            -PREMATCH_MAX_ABS_STANDARDIZED_VALUE,
            PREMATCH_MAX_ABS_STANDARDIZED_VALUE,
        )
        missing_counts.append((name, sum(value is None for value in column)))
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


def offset_logistic_objective_and_gradient(
    parameters: np.ndarray,
    matrix: np.ndarray,
    offsets: np.ndarray,
    outcomes: np.ndarray,
    l2_regularization: float,
) -> tuple[float, np.ndarray]:
    """Return the fixed-offset penalized likelihood and analytic gradient."""

    theta = np.asarray(parameters, dtype=np.float64)
    design = np.asarray(matrix, dtype=np.float64)
    base = np.asarray(offsets, dtype=np.float64)
    observed = np.asarray(outcomes, dtype=np.float64)
    if theta.ndim != 1 or design.ndim != 2:
        raise ValueError("parameters and matrix have invalid dimensions")
    if design.shape[0] != base.shape[0] or base.shape != observed.shape:
        raise ValueError("matrix, offsets, and outcomes have incompatible shapes")
    if theta.shape[0] != design.shape[1] + 1:
        raise ValueError("parameter vector does not match matrix")
    regularization = _finite(l2_regularization, "l2_regularization")
    if regularization <= 0.0:
        raise ValueError("l2_regularization must be positive")
    intercept = theta[0]
    coefficients = theta[1:]
    logits = base + intercept + design @ coefficients
    residual = expit(logits) - observed
    loss = float(
        np.sum(np.logaddexp(0.0, logits) - observed * logits)
        + 0.5 * regularization * float(coefficients @ coefficients)
    )
    gradient = np.concatenate(
        (
            np.asarray((float(np.sum(residual)),)),
            design.T @ residual + regularization * coefficients,
        )
    )
    return loss, gradient


def _artifact(
    *,
    model_kind: str,
    status: ModelStatus,
    reason: str | None,
    availability_mode: str,
    training_cutoff: datetime,
    support: int,
    series_support: int,
    event_support: int,
    patch_support: int,
    min_samples: int,
    l2_regularization: float,
    training_input_hash: str,
    training_corpus: tuple[PrematchTrainingCorpusRow, ...],
    class_counts: tuple[tuple[int, int], ...],
    missing_counts: tuple[tuple[str, int], ...],
    imputation_values: tuple[tuple[str, float], ...],
    means: tuple[tuple[str, float], ...],
    scales: tuple[tuple[str, float], ...],
    coefficients: tuple[tuple[str, float], ...] = (),
    intercept: float | None = None,
    covariance: tuple[tuple[float, ...], ...] = (),
) -> PrematchModelArtifact:
    artifact = PrematchModelArtifact(
        artifact_version=PREMATCH_MODEL_ARTIFACT_VERSION,
        model_version=PREMATCH_MODEL_VERSION,
        feature_version=PREMATCH_FEATURE_VERSION,
        model_kind=model_kind,
        status=status,
        reason=reason,
        availability_mode=availability_mode,
        training_cutoff=training_cutoff,
        support=support,
        series_support=series_support,
        event_support=event_support,
        patch_support=patch_support,
        min_samples=min_samples,
        l2_regularization=l2_regularization,
        feature_names=prematch_feature_schema(model_kind),
        feature_schema_hash=prematch_feature_schema_hash(model_kind),
        training_input_hash=training_input_hash,
        trainer_runtime=PREMATCH_TRAINER_RUNTIME,
        training_corpus=training_corpus,
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
        model_hash=_hash(artifact.to_payload(include_model_hash=False)),
    )


def fit_prematch_model(
    rows: Iterable[PrematchTrainingRow],
    training_cutoff: datetime,
    *,
    model_kind: str,
    availability_mode: str,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    l2_regularization: float = DEFAULT_L2_REGULARIZATION,
) -> PrematchModelArtifact:
    """Fit eligible rows only, keeping Team Rating as an immutable offset."""

    prematch_feature_schema(model_kind)
    mode = _mode(availability_mode)
    cutoff = _utc(training_cutoff, "training_cutoff")
    if (
        isinstance(min_samples, bool)
        or not isinstance(min_samples, int)
        or min_samples < 2
    ):
        raise ValueError("min_samples must be an integer of at least 2")
    regularization = _finite(l2_regularization, "l2_regularization")
    if regularization <= 0.0:
        raise ValueError("l2_regularization must be positive")

    prepared_by_match: dict[int, PreparedRow] = {}
    for row in rows:
        if not isinstance(row, PrematchTrainingRow):
            raise ValueError("rows must contain PrematchTrainingRow values")
        prediction_cutoff = _utc(row.prediction_cutoff, "row prediction_cutoff")
        if prediction_cutoff >= cutoff:
            continue
        completed_at = _utc(row.completed_at, "row completed_at")
        if completed_at >= cutoff:
            continue
        if row.result_usable_at is None:
            continue
        result_usable_at = _utc(row.result_usable_at, "row result_usable_at")
        if result_usable_at > cutoff:
            continue

        if completed_at <= prediction_cutoff:
            raise ValueError("row completed_at must follow prediction_cutoff")
        if result_usable_at < completed_at:
            raise ValueError("row result_usable_at cannot precede completed_at")
        if _mode(row.availability_mode) != mode:
            raise ValueError("training rows cannot mix availability modes")
        match_id = _positive_int(row.match_id, "match_id")
        snapshot_hash = _digest(row.input_snapshot_hash, "input_snapshot_hash")
        outcome = _binary(row.outcome)
        series_id = _nonempty(str(row.series_id).strip(), "series_id")
        event_id = _nonempty(row.event_id, "event_id")
        patch_id = _nonempty(row.patch_id, "patch_id")
        offset = _finite(row.team_base_logit, "team_base_logit")
        values, missing = _align_features(model_kind, row.features)
        normalized = replace(
            row,
            match_id=match_id,
            input_snapshot_hash=snapshot_hash,
            prediction_cutoff=prediction_cutoff,
            completed_at=completed_at,
            result_usable_at=result_usable_at,
            availability_mode=mode,
            outcome=outcome,
            series_id=series_id,
            event_id=event_id,
            patch_id=patch_id,
            team_base_logit=offset,
        )
        candidate = (normalized, values, missing)
        existing = prepared_by_match.get(match_id)
        if existing is not None:
            if _prepared_payload(existing, model_kind) != _prepared_payload(
                candidate,
                model_kind,
            ):
                raise ValueError(f"conflicting training rows for match {match_id}")
            continue
        prepared_by_match[match_id] = candidate

    prepared = tuple(
        sorted(
            prepared_by_match.values(),
            key=lambda item: (
                item[0].prediction_cutoff,
                item[0].completed_at,
                item[0].result_usable_at,
                item[0].match_id,
                str(item[0].series_id),
            ),
        )
    )
    support = len(prepared)
    counts = {0: 0, 1: 0}
    for row, _values, _missing in prepared:
        counts[int(row.outcome)] += 1
    class_counts = tuple(sorted(counts.items()))
    corpus = tuple(_corpus_row(row, model_kind) for row in prepared)
    training_input_hash = _hash(
        {
            "model_version": PREMATCH_MODEL_VERSION,
            "feature_version": PREMATCH_FEATURE_VERSION,
            "model_kind": model_kind,
            "availability_mode": mode,
            "training_cutoff": cutoff.isoformat(),
            "feature_schema_hash": prematch_feature_schema_hash(model_kind),
            "rows": [row.to_payload() for row in corpus],
        }
    )
    matrix, missing_counts, imputation, means, scales = _column_statistics(
        prepared,
        model_kind,
    )
    common = {
        "model_kind": model_kind,
        "availability_mode": mode,
        "training_cutoff": cutoff,
        "support": support,
        "series_support": len({str(row.series_id) for row, *_rest in prepared}),
        "event_support": len({row.event_id for row, *_rest in prepared}),
        "patch_support": len({row.patch_id for row, *_rest in prepared}),
        "min_samples": min_samples,
        "l2_regularization": regularization,
        "training_input_hash": training_input_hash,
        "training_corpus": corpus,
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

    offsets = np.asarray(
        [row.team_base_logit for row, _values, _missing in prepared],
        dtype=np.float64,
    )
    outcomes = np.asarray(
        [int(row.outcome) for row, _values, _missing in prepared],
        dtype=np.float64,
    )
    initial = np.zeros(matrix.shape[1] + 1, dtype=np.float64)
    result = minimize(
        offset_logistic_objective_and_gradient,
        initial,
        args=(matrix, offsets, outcomes, regularization),
        method=PREMATCH_SOLVER_METHOD,
        jac=True,
        options={
            "maxiter": PREMATCH_MAX_ITERATIONS,
            "ftol": PREMATCH_FTOL,
            "gtol": PREMATCH_GTOL,
            "maxls": PREMATCH_MAX_LINE_SEARCH_STEPS,
        },
    )
    if not result.success:
        raise RuntimeError(
            f"prematch optimizer failed ({result.status}): {result.message}"
        )
    parameters = np.asarray(result.x, dtype=np.float64)
    if not np.all(np.isfinite(parameters)) or not math.isfinite(float(result.fun)):
        raise RuntimeError("prematch optimizer returned nonfinite parameters")
    intercept = float(parameters[0])
    coefficient_values = tuple(float(value) for value in parameters[1:])

    logits = offsets + intercept + matrix @ parameters[1:]
    probabilities = expit(logits)
    augmented = np.column_stack((np.ones(support), matrix))
    weights = probabilities * (1.0 - probabilities)
    hessian = augmented.T @ (augmented * weights[:, None])
    if matrix.shape[1]:
        hessian[1:, 1:] += regularization * np.eye(matrix.shape[1])
    covariance_array = np.linalg.pinv(
        hessian,
        rcond=PREMATCH_PINV_RCOND,
        hermitian=True,
    )
    covariance_array = (covariance_array + covariance_array.T) / 2.0
    if not np.all(np.isfinite(covariance_array)):
        raise RuntimeError("prematch covariance is nonfinite")
    covariance = tuple(tuple(float(value) for value in row) for row in covariance_array)
    return _artifact(
        **common,
        status=ModelStatus.TRAINED,
        reason=None,
        coefficients=tuple(
            zip(
                prematch_feature_schema(model_kind),
                coefficient_values,
                strict=True,
            )
        ),
        intercept=intercept,
        covariance=covariance,
    )


def verify_prematch_model_artifact(model: PrematchModelArtifact) -> None:
    if not isinstance(model, PrematchModelArtifact):
        raise ValueError("model must be a PrematchModelArtifact")
    if model.artifact_version != PREMATCH_MODEL_ARTIFACT_VERSION:
        raise ValueError("unsupported prematch model artifact version")
    if model.model_version != PREMATCH_MODEL_VERSION:
        raise ValueError("unsupported prematch model version")
    if model.feature_version != PREMATCH_FEATURE_VERSION:
        raise ValueError("unsupported prematch feature version")
    schema = prematch_feature_schema(model.model_kind)
    if model.feature_names != schema or model.feature_schema_hash != (
        prematch_feature_schema_hash(model.model_kind)
    ):
        raise ValueError("prematch feature schema does not recompute")
    mode = _mode(model.availability_mode)
    cutoff = _utc(model.training_cutoff, "training_cutoff")
    if model.trainer_runtime != PREMATCH_TRAINER_RUNTIME:
        raise ValueError("prematch trainer runtime does not match")
    if (
        isinstance(model.support, bool)
        or not isinstance(model.support, int)
        or model.support < 0
        or len(model.training_corpus) != model.support
        or isinstance(model.min_samples, bool)
        or not isinstance(model.min_samples, int)
        or model.min_samples < 2
    ):
        raise ValueError("prematch support metadata is invalid")
    for name, value in (
        ("series_support", model.series_support),
        ("event_support", model.event_support),
        ("patch_support", model.patch_support),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= model.support
        ):
            raise ValueError(f"prematch {name} is invalid")
    regularization = _finite(model.l2_regularization, "l2_regularization")
    if regularization <= 0.0:
        raise ValueError("l2_regularization must be positive")
    _digest(model.training_input_hash, "training_input_hash")
    expected_names = schema
    for field, rows in (
        ("missing_counts", model.missing_counts),
        ("imputation_values", model.imputation_values),
        ("standardization_means", model.standardization_means),
        ("standardization_scales", model.standardization_scales),
    ):
        if tuple(name for name, _value in rows) != expected_names:
            raise ValueError(f"prematch {field} do not match feature schema")
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= model.support
        for _name, value in model.missing_counts
    ):
        raise ValueError("prematch missing counts are invalid")
    for field, rows in (
        ("imputation", model.imputation_values),
        ("standardization mean", model.standardization_means),
        ("standardization scale", model.standardization_scales),
        ("coefficient", model.coefficients),
    ):
        for _name, value in rows:
            _finite(value, field)
    if any(value <= 0.0 for _name, value in model.standardization_scales):
        raise ValueError("prematch standardization scales must be positive")
    if any(
        value < PREMATCH_MIN_STANDARDIZATION_SCALE
        for name, value in model.standardization_scales
        if not name.endswith("__missing")
    ):
        raise ValueError("prematch standardization scales are below the floor")
    means = dict(model.standardization_means)
    scales = dict(model.standardization_scales)
    imputation = dict(model.imputation_values)
    if any(
        means[name] != 0.0 or scales[name] != 1.0 or imputation[name] != 0.0
        for name in schema
        if name.endswith("__missing")
    ):
        raise ValueError("prematch missing flags must remain unstandardized")
    counts = dict(model.class_counts)
    if (
        set(counts) != {0, 1}
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts.values()
        )
        or sum(counts.values()) != model.support
    ):
        raise ValueError("prematch class counts do not match support")

    if model.status is ModelStatus.TRAINED:
        if (
            model.reason is not None
            or model.support < model.min_samples
            or not counts[0]
            or not counts[1]
            or tuple(name for name, _value in model.coefficients) != schema
            or model.intercept is None
        ):
            raise ValueError("trained prematch parameters are incomplete")
        _finite(model.intercept, "intercept")
        covariance = np.asarray(model.logit_covariance, dtype=np.float64)
        expected_shape = (len(schema) + 1, len(schema) + 1)
        if covariance.shape != expected_shape or not np.all(np.isfinite(covariance)):
            raise ValueError("prematch covariance is incomplete")
        if not np.allclose(covariance, covariance.T, rtol=0.0, atol=1e-12):
            raise ValueError("prematch covariance must be symmetric")
        if float(np.min(np.linalg.eigvalsh(covariance))) < -1e-10:
            raise ValueError("prematch covariance must be positive semidefinite")
    else:
        expected_reason = (
            "support_below_minimum"
            if model.support < model.min_samples
            else "single_class_training_data"
            if not counts[0] or not counts[1]
            else None
        )
        if (
            expected_reason is None
            or model.reason != expected_reason
            or model.coefficients
            or model.intercept is not None
            or model.logit_covariance
        ):
            raise ValueError("insufficient prematch parameters are inconsistent")

    claimed = _digest(model.model_hash, "model_hash")
    expected_hash = _hash(model.to_payload(include_model_hash=False))
    if not hmac.compare_digest(claimed, expected_hash):
        raise ValueError("prematch model hash does not match")
    replayed = fit_prematch_model(
        (row.to_training_row() for row in model.training_corpus),
        cutoff,
        model_kind=model.model_kind,
        availability_mode=mode,
        min_samples=model.min_samples,
        l2_regularization=regularization,
    )
    expected_payload = canonical_json_bytes(
        replayed.to_payload(include_model_hash=False)
    )
    actual_payload = canonical_json_bytes(model.to_payload(include_model_hash=False))
    if not hmac.compare_digest(actual_payload, expected_payload):
        raise ValueError("prematch model does not replay from its training corpus")


def predict_prematch(
    model: PrematchModelArtifact,
    snapshot: PrematchFeatureSnapshot,
    *,
    top_n: int = 5,
) -> PrematchPrediction:
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 0:
        raise ValueError("top_n must be a nonnegative integer")
    verify_prematch_model_artifact(model)
    verify_prematch_feature_snapshot(snapshot)
    if model.availability_mode != snapshot.availability_mode:
        raise ValueError("model and snapshot availability modes disagree")
    if model.training_cutoff > snapshot.prediction_cutoff:
        raise ValueError("model training cutoff follows prediction cutoff")
    if any(row.match_id == snapshot.match_id for row in model.training_corpus):
        raise ValueError("target match entered the prematch training corpus")
    features = project_prematch_features(snapshot, model.model_kind)
    values, missing = _align_features(model.model_kind, features)
    if model.status is not ModelStatus.TRAINED:
        return PrematchPrediction(
            status=PredictionStatus.INSUFFICIENT_EVIDENCE,
            reason=model.reason,
            raw_probability=None,
            parameter_uncertainty=None,
            team_base_logit=snapshot.team_base_logit,
            learned_intercept=None,
            draft_logit_delta=None,
            rosh_logit_delta=None,
            cluster_logit_delta=None,
            total_adjustment=None,
            support=model.support,
            model_hash=model.model_hash,
            input_snapshot_hash=snapshot.input_hash,
            missing_features=missing,
            top_contributions=(),
            top_cluster_contributions=(),
        )

    coefficients = dict(model.coefficients)
    imputation = dict(model.imputation_values)
    means = dict(model.standardization_means)
    scales = dict(model.standardization_scales)
    if model.intercept is None or set(coefficients) != set(model.feature_names):
        raise ValueError("trained prematch parameters are incomplete")
    standardized_values: list[float] = []
    contributions: list[FeatureContribution] = []
    for name, raw in zip(model.feature_names, values, strict=True):
        was_imputed = raw is None
        value = imputation[name] if was_imputed else raw
        if value is None:
            raise ValueError("prematch imputation parameters are incomplete")
        standardized = max(
            -PREMATCH_MAX_ABS_STANDARDIZED_VALUE,
            min(
                PREMATCH_MAX_ABS_STANDARDIZED_VALUE,
                (value - means[name]) / scales[name],
            ),
        )
        contribution = coefficients[name] * standardized
        component = (
            "draft"
            if name in DRAFT_RESIDUAL_MODEL_SCHEMA
            else "rosh"
            if name in ROSH_MODEL_SCHEMA
            else "cluster"
        )
        if component == "cluster" and name not in PREMATCH_CLUSTER_MODEL_SCHEMA:
            raise ValueError("prematch feature has no component")
        standardized_values.append(standardized)
        contributions.append(
            FeatureContribution(
                feature_name=name,
                component=component,
                input_value=value,
                standardized_value=standardized,
                coefficient=coefficients[name],
                log_odds_contribution=contribution,
                was_imputed=was_imputed,
            )
        )
    includes_draft = any(
        name in DRAFT_RESIDUAL_MODEL_SCHEMA for name in model.feature_names
    )
    includes_rosh = any(name in ROSH_MODEL_SCHEMA for name in model.feature_names)
    includes_cluster = any(
        name in PREMATCH_CLUSTER_MODEL_SCHEMA for name in model.feature_names
    )
    draft_delta = (
        math.fsum(
            row.log_odds_contribution
            for row in contributions
            if row.feature_name in DRAFT_RESIDUAL_MODEL_SCHEMA
        )
        if includes_draft
        else None
    )
    rosh_delta = (
        math.fsum(
            row.log_odds_contribution
            for row in contributions
            if row.feature_name in ROSH_MODEL_SCHEMA
        )
        if includes_rosh
        else None
    )
    cluster_delta = (
        math.fsum(
            row.log_odds_contribution
            for row in contributions
            if row.feature_name in PREMATCH_CLUSTER_MODEL_SCHEMA
        )
        if includes_cluster
        else None
    )
    total_adjustment = (
        model.intercept
        + (draft_delta or 0.0)
        + (rosh_delta or 0.0)
        + (cluster_delta or 0.0)
    )
    probability = float(expit(snapshot.team_base_logit + total_adjustment))
    covariance = np.asarray(model.logit_covariance, dtype=np.float64)
    design = np.asarray((1.0, *standardized_values), dtype=np.float64)
    raw_variance = float(design @ covariance @ design)
    if raw_variance < -1e-10:
        raise ValueError("prematch covariance produced negative variance")
    uncertainty = probability * (1.0 - probability) * math.sqrt(max(0.0, raw_variance))
    ranked = tuple(
        sorted(
            (
                row
                for row in contributions
                if not row.feature_name.endswith("__missing")
            ),
            key=lambda row: (-abs(row.log_odds_contribution), row.feature_name),
        )[:top_n]
    )
    ranked_cluster = tuple(
        sorted(
            (
                row
                for row in contributions
                if row.component == "cluster"
                and not row.feature_name.endswith("__missing")
            ),
            key=lambda row: (-abs(row.log_odds_contribution), row.feature_name),
        )[:top_n]
    )
    return PrematchPrediction(
        status=PredictionStatus.PREDICTED,
        reason=None,
        raw_probability=probability,
        parameter_uncertainty=uncertainty,
        team_base_logit=snapshot.team_base_logit,
        learned_intercept=model.intercept,
        draft_logit_delta=draft_delta,
        rosh_logit_delta=rosh_delta,
        cluster_logit_delta=cluster_delta,
        total_adjustment=total_adjustment,
        support=model.support,
        model_hash=model.model_hash,
        input_snapshot_hash=snapshot.input_hash,
        missing_features=missing,
        top_contributions=ranked,
        top_cluster_contributions=ranked_cluster,
    )


__all__ = [
    "DEFAULT_L2_REGULARIZATION",
    "DEFAULT_MIN_SAMPLES",
    "PREMATCH_FTOL",
    "PREMATCH_GTOL",
    "PREMATCH_MAX_ITERATIONS",
    "PREMATCH_MAX_LINE_SEARCH_STEPS",
    "PREMATCH_MAX_ABS_STANDARDIZED_VALUE",
    "PREMATCH_MIN_FEATURE_NONMISSING_SUPPORT",
    "PREMATCH_MIN_STANDARDIZATION_SCALE",
    "PREMATCH_MODEL_ARTIFACT_VERSION",
    "PREMATCH_MODEL_VERSION",
    "PREMATCH_PINV_RCOND",
    "PREMATCH_SOLVER_METHOD",
    "PREMATCH_TRAINER_RUNTIME",
    "FeatureContribution",
    "ModelStatus",
    "PredictionStatus",
    "PrematchModelArtifact",
    "PrematchPrediction",
    "PrematchTrainingCorpusRow",
    "PrematchTrainingRow",
    "fit_prematch_model",
    "offset_logistic_objective_and_gradient",
    "predict_prematch",
    "verify_prematch_model_artifact",
]
