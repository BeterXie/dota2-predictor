"""Chronological out-of-sample Platt calibration for prematch predictions."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import platform
import random
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from numbers import Real
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import scipy
from scipy.optimize import minimize
from scipy.special import expit

from .draft_features import AvailabilityMode
from .draft_model import (
    BinaryMetrics,
    CalibrationBin,
    evaluate_binary_predictions,
)
from .prematch_features import PREMATCH_MODEL_KINDS
from .raw_archive import canonical_json_bytes


UTC = timezone.utc
PREMATCH_CALIBRATION_VERSION = "prematch-platt-v1"
PREMATCH_CALIBRATION_ARTIFACT_SCHEMA = "prematch-calibration-artifact/v1"
CALIBRATION_MIN_FIT_SUPPORT = 20
CALIBRATION_MIN_EVALUATION_SUPPORT = 100
CALIBRATION_ECE_BINS = 5
CALIBRATION_MAX_BRIER = 0.25
CALIBRATION_MAX_LOG_LOSS = math.log(2.0)
CALIBRATION_MAX_ECE = 0.10
CALIBRATION_MAX_ECE_90_UPPER = 0.15
CALIBRATION_BOOTSTRAP_SAMPLES = 1_000
CALIBRATION_BOOTSTRAP_CONFIDENCE = 0.90
CALIBRATION_BOOTSTRAP_ALGORITHM = "series-cluster-percentile-v1"
CALIBRATION_PROBABILITY_EPSILON = 1e-15
CALIBRATION_SOLVER_METHOD = "L-BFGS-B"
CALIBRATION_MAX_ITERATIONS = 2_000
CALIBRATION_FTOL = 1e-12
CALIBRATION_GTOL = 1e-8
CALIBRATION_MAX_LINE_SEARCH_STEPS = 50
CALIBRATION_PARAMETER_BOUND = 20.0
CALIBRATION_RUNTIME = (
    ("numpy", np.__version__),
    ("python_implementation", platform.python_implementation()),
    ("python_version", platform.python_version()),
    ("scipy", scipy.__version__),
)
CALIBRATION_SOLVER = (
    ("method", CALIBRATION_SOLVER_METHOD),
    ("max_iterations", CALIBRATION_MAX_ITERATIONS),
    ("ftol", CALIBRATION_FTOL),
    ("gtol", CALIBRATION_GTOL),
    ("max_line_search_steps", CALIBRATION_MAX_LINE_SEARCH_STEPS),
    ("parameter_bound", CALIBRATION_PARAMETER_BOUND),
    ("probability_epsilon", CALIBRATION_PROBABILITY_EPSILON),
)


class CalibrationStatus(str, Enum):
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    PROVISIONAL = "provisional"
    RECONSTRUCTED_ONLY = "reconstructed_only"
    SHADOW_COLLECTING = "shadow_collecting"
    PASSED = "passed"


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


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    return _utc(parsed, field)


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _probability(value: object, field: str) -> float:
    result = _finite(value, field)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{field} must be between zero and one")
    return result


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
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
        raise ValueError("unsupported calibration availability mode") from error


def _model_kind(value: object) -> str:
    kind = _nonempty(value, "model_kind")
    if kind not in PREMATCH_MODEL_KINDS:
        raise ValueError("unsupported prematch model kind")
    return kind


def _sample_key(sample: PrematchCalibrationSample) -> tuple[datetime, int]:
    return sample.prediction_cutoff, sample.match_id


@dataclass(frozen=True)
class PrematchCalibrationSample:
    match_id: int
    series_id: str
    event_id: str
    patch_id: str
    model_kind: str
    availability_mode: str
    prediction_cutoff: datetime
    result_usable_at: datetime
    raw_probability: float
    outcome: int
    model_hash: str
    input_snapshot_hash: str
    is_out_of_sample: bool = True

    def __post_init__(self) -> None:
        _positive_int(self.match_id, "calibration match_id")
        for name in ("series_id", "event_id", "patch_id"):
            object.__setattr__(self, name, _nonempty(getattr(self, name), name))
        object.__setattr__(self, "model_kind", _model_kind(self.model_kind))
        object.__setattr__(self, "availability_mode", _mode(self.availability_mode))
        prediction_cutoff = _utc(self.prediction_cutoff, "prediction_cutoff")
        result_usable_at = _utc(self.result_usable_at, "result_usable_at")
        if result_usable_at <= prediction_cutoff:
            raise ValueError(
                "calibration result must be usable after prediction cutoff"
            )
        object.__setattr__(self, "prediction_cutoff", prediction_cutoff)
        object.__setattr__(self, "result_usable_at", result_usable_at)
        object.__setattr__(
            self,
            "raw_probability",
            _probability(self.raw_probability, "raw_probability"),
        )
        object.__setattr__(self, "outcome", _binary(self.outcome))
        for name in ("model_hash", "input_snapshot_hash"):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if self.is_out_of_sample is not True:
            raise ValueError(
                "calibration samples must be raw out-of-sample predictions"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "match_id": self.match_id,
            "series_id": self.series_id,
            "event_id": self.event_id,
            "patch_id": self.patch_id,
            "model_kind": self.model_kind,
            "availability_mode": self.availability_mode,
            "prediction_cutoff": self.prediction_cutoff.isoformat(),
            "result_usable_at": self.result_usable_at.isoformat(),
            "raw_probability": self.raw_probability,
            "outcome": self.outcome,
            "model_hash": self.model_hash,
            "input_snapshot_hash": self.input_snapshot_hash,
            "is_out_of_sample": self.is_out_of_sample,
        }


@dataclass(frozen=True)
class PrematchCalibrationApplication:
    status: CalibrationStatus
    reason: str | None
    model_kind: str
    availability_mode: str
    prediction_cutoff: datetime
    raw_probability: float
    calibrated_probability: float | None
    model_hash: str
    input_snapshot_hash: str
    calibration_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, CalibrationStatus):
            raise ValueError("calibration application status is invalid")
        if self.reason is not None:
            _nonempty(self.reason, "reason")
        _model_kind(self.model_kind)
        _mode(self.availability_mode)
        _utc(self.prediction_cutoff, "prediction_cutoff")
        _probability(self.raw_probability, "raw_probability")
        if self.calibrated_probability is not None:
            _probability(self.calibrated_probability, "calibrated_probability")
        for name in ("model_hash", "input_snapshot_hash", "calibration_hash"):
            _digest(getattr(self, name), name)


@dataclass(frozen=True)
class PrematchCalibrationArtifact:
    artifact_schema: str
    calibration_version: str
    model_kind: str
    availability_mode: str
    status: CalibrationStatus
    reason: str | None
    calibration_cutoff: datetime
    fit_cutoff: datetime | None
    evaluation_start: datetime | None
    fit_support: int
    evaluation_support: int
    fit_series_ids: tuple[str, ...]
    evaluation_series_ids: tuple[str, ...]
    parameters: tuple[float, float] | None
    raw_metrics: BinaryMetrics | None
    calibrated_metrics: BinaryMetrics | None
    ece_90_upper: float | None
    gate_passed: bool
    gate_reasons: tuple[str, ...]
    origin_model_hashes: tuple[str, ...]
    oos_stream_hash: str
    input_hash: str
    runtime_versions: tuple[tuple[str, str], ...]
    solver: tuple[tuple[str, object], ...]
    bootstrap_algorithm: str
    bootstrap_samples: int
    bootstrap_confidence: float
    bootstrap_seed: str
    oos_samples: tuple[PrematchCalibrationSample, ...]
    fit_samples: tuple[PrematchCalibrationSample, ...]
    evaluation_samples: tuple[PrematchCalibrationSample, ...]
    calibration_hash: str

    def to_payload(self, *, include_calibration_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "artifact_schema": self.artifact_schema,
            "calibration_version": self.calibration_version,
            "model_kind": self.model_kind,
            "availability_mode": self.availability_mode,
            "status": self.status.value,
            "reason": self.reason,
            "calibration_cutoff": self.calibration_cutoff.isoformat(),
            "fit_cutoff": (
                None if self.fit_cutoff is None else self.fit_cutoff.isoformat()
            ),
            "evaluation_start": (
                None
                if self.evaluation_start is None
                else self.evaluation_start.isoformat()
            ),
            "fit_support": self.fit_support,
            "evaluation_support": self.evaluation_support,
            "fit_series_ids": list(self.fit_series_ids),
            "evaluation_series_ids": list(self.evaluation_series_ids),
            "parameters": (
                None
                if self.parameters is None
                else {"a": self.parameters[0], "b": self.parameters[1]}
            ),
            "raw_metrics": (
                None if self.raw_metrics is None else self.raw_metrics.to_payload()
            ),
            "calibrated_metrics": (
                None
                if self.calibrated_metrics is None
                else self.calibrated_metrics.to_payload()
            ),
            "ece_90_upper": self.ece_90_upper,
            "gate_passed": self.gate_passed,
            "gate_reasons": list(self.gate_reasons),
            "origin_model_hashes": list(self.origin_model_hashes),
            "oos_stream_hash": self.oos_stream_hash,
            "input_hash": self.input_hash,
            "runtime_versions": dict(self.runtime_versions),
            "solver": dict(self.solver),
            "bootstrap_algorithm": self.bootstrap_algorithm,
            "bootstrap_samples": self.bootstrap_samples,
            "bootstrap_confidence": self.bootstrap_confidence,
            "bootstrap_seed": self.bootstrap_seed,
            "oos_samples": [row.to_payload() for row in self.oos_samples],
            "fit_samples": [row.to_payload() for row in self.fit_samples],
            "evaluation_samples": [row.to_payload() for row in self.evaluation_samples],
        }
        if include_calibration_hash:
            payload["calibration_hash"] = self.calibration_hash
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())


@dataclass(frozen=True)
class _SeriesSplit:
    fit: tuple[PrematchCalibrationSample, ...]
    evaluation: tuple[PrematchCalibrationSample, ...]
    reason: str | None


def _canonical_oos_stream(
    samples: Iterable[PrematchCalibrationSample],
    *,
    calibration_cutoff: datetime,
    model_kind: str,
    availability_mode: str,
) -> tuple[PrematchCalibrationSample, ...]:
    eligible: list[PrematchCalibrationSample] = []
    for sample in samples:
        if not isinstance(sample, PrematchCalibrationSample):
            raise ValueError(
                "calibration stream requires PrematchCalibrationSample values"
            )
        if (
            sample.prediction_cutoff >= calibration_cutoff
            or sample.result_usable_at > calibration_cutoff
        ):
            continue
        if sample.model_kind != model_kind:
            raise ValueError("calibration stream mixes model kinds")
        if sample.availability_mode != availability_mode:
            raise ValueError("calibration stream mixes availability modes")
        eligible.append(sample)
    ordered = tuple(sorted(eligible, key=_sample_key))
    match_ids = tuple(row.match_id for row in ordered)
    if len(set(match_ids)) != len(match_ids):
        raise ValueError("calibration stream contains duplicate match IDs")
    series_metadata: dict[str, tuple[str, str]] = {}
    for row in ordered:
        metadata = (row.event_id, row.patch_id)
        previous = series_metadata.setdefault(row.series_id, metadata)
        if previous != metadata:
            raise ValueError("one calibration series spans event or patch identities")
    return ordered


def _series_groups(
    stream: Sequence[PrematchCalibrationSample],
) -> tuple[tuple[PrematchCalibrationSample, ...], ...]:
    grouped: dict[str, list[PrematchCalibrationSample]] = {}
    for row in stream:
        grouped.setdefault(row.series_id, []).append(row)
    return tuple(
        tuple(grouped[series_id])
        for series_id in sorted(
            grouped,
            key=lambda series_id: (
                min(row.prediction_cutoff for row in grouped[series_id]),
                series_id,
            ),
        )
    )


def _has_both_classes(rows: Sequence[PrematchCalibrationSample]) -> bool:
    return {row.outcome for row in rows} == {0, 1}


def _choose_series_split(
    stream: Sequence[PrematchCalibrationSample],
) -> _SeriesSplit:
    groups = _series_groups(stream)
    if len(groups) < 2:
        return _SeriesSplit((), (), "no_legal_series_split")
    causal: list[
        tuple[
            tuple[PrematchCalibrationSample, ...],
            tuple[PrematchCalibrationSample, ...],
        ]
    ] = []
    for boundary in range(1, len(groups)):
        fit_series = {row.series_id for group in groups[:boundary] for row in group}
        evaluation_series = {
            row.series_id for group in groups[boundary:] for row in group
        }
        fit = tuple(row for row in stream if row.series_id in fit_series)
        evaluation = tuple(row for row in stream if row.series_id in evaluation_series)
        fit_usable_at = max(row.result_usable_at for row in fit)
        evaluation_start = min(row.prediction_cutoff for row in evaluation)
        if fit_usable_at < evaluation_start:
            causal.append((fit, evaluation))
    if not causal:
        return _SeriesSplit((), (), "no_legal_series_split")
    supported = [pair for pair in causal if len(pair[0]) >= CALIBRATION_MIN_FIT_SUPPORT]
    if not supported:
        fit, evaluation = causal[0]
        return _SeriesSplit(fit, evaluation, "fit_support_below_20")
    for fit, evaluation in supported:
        if _has_both_classes(fit) and _has_both_classes(evaluation):
            return _SeriesSplit(fit, evaluation, None)
    fit, evaluation = supported[0]
    return _SeriesSplit(fit, evaluation, "single_class_split")


def _logit(probability: float) -> float:
    clipped = min(
        1.0 - CALIBRATION_PROBABILITY_EPSILON,
        max(CALIBRATION_PROBABILITY_EPSILON, probability),
    )
    return math.log(clipped) - math.log1p(-clipped)


def _calibrated_probability(parameters: tuple[float, float], raw: float) -> float:
    a, b = parameters
    return float(expit(a + b * _logit(raw)))


def _fit_platt(
    rows: Sequence[PrematchCalibrationSample],
) -> tuple[float, float] | None:
    logits = np.asarray([_logit(row.raw_probability) for row in rows], dtype=np.float64)
    outcomes = np.asarray([row.outcome for row in rows], dtype=np.float64)

    def objective(parameters: np.ndarray) -> tuple[float, np.ndarray]:
        scores = parameters[0] + parameters[1] * logits
        probabilities = expit(scores)
        loss = float(np.sum(np.logaddexp(0.0, scores) - outcomes * scores))
        residual = probabilities - outcomes
        gradient = np.asarray(
            (float(np.sum(residual)), float(np.dot(residual, logits))),
            dtype=np.float64,
        )
        return loss, gradient

    result = minimize(
        objective,
        np.asarray((0.0, 1.0), dtype=np.float64),
        method=CALIBRATION_SOLVER_METHOD,
        jac=True,
        bounds=(
            (-CALIBRATION_PARAMETER_BOUND, CALIBRATION_PARAMETER_BOUND),
            (-CALIBRATION_PARAMETER_BOUND, CALIBRATION_PARAMETER_BOUND),
        ),
        options={
            "maxiter": CALIBRATION_MAX_ITERATIONS,
            "ftol": CALIBRATION_FTOL,
            "gtol": CALIBRATION_GTOL,
            "maxls": CALIBRATION_MAX_LINE_SEARCH_STEPS,
        },
    )
    if (
        not result.success
        or result.x.shape != (2,)
        or not np.all(np.isfinite(result.x))
    ):
        return None
    return float(result.x[0]), float(result.x[1])


def _metrics(
    rows: Sequence[PrematchCalibrationSample],
    probabilities: Sequence[float],
) -> BinaryMetrics:
    return evaluate_binary_predictions(
        (row.outcome for row in rows),
        probabilities,
        ece_bins=CALIBRATION_ECE_BINS,
    )


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _bootstrap_ece_upper(
    rows: Sequence[PrematchCalibrationSample],
    probabilities: Sequence[float],
    *,
    seed: str,
    point_ece: float,
) -> float:
    grouped: dict[str, list[tuple[PrematchCalibrationSample, float]]] = {}
    for row, probability in zip(rows, probabilities, strict=True):
        grouped.setdefault(row.series_id, []).append((row, probability))
    keys = sorted(grouped)
    generator = random.Random(int(seed[:16], 16))
    estimates: list[float] = []
    for _sample in range(CALIBRATION_BOOTSTRAP_SAMPLES):
        sampled = [grouped[keys[generator.randrange(len(keys))]] for _ in keys]
        flat = tuple(item for cluster in sampled for item in cluster)
        metrics = evaluate_binary_predictions(
            (row.outcome for row, _probability_value in flat),
            (probability_value for _row, probability_value in flat),
            ece_bins=CALIBRATION_ECE_BINS,
        )
        if metrics.expected_calibration_error is not None:
            estimates.append(metrics.expected_calibration_error)
    upper_quantile = 1.0 - (1.0 - CALIBRATION_BOOTSTRAP_CONFIDENCE) / 2.0
    upper = _percentile(estimates, upper_quantile)
    if upper is None:
        raise ValueError("series-cluster bootstrap produced no ECE estimates")
    return max(point_ece, upper)


def _gate(
    metrics: BinaryMetrics,
    ece_90_upper: float,
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if metrics.support < CALIBRATION_MIN_EVALUATION_SUPPORT:
        reasons.append("evaluation_support_below_100")
    if metrics.brier_score is None or metrics.brier_score >= CALIBRATION_MAX_BRIER:
        reasons.append("brier_not_below_0.25")
    if metrics.log_loss is None or metrics.log_loss >= CALIBRATION_MAX_LOG_LOSS:
        reasons.append("log_loss_not_below_ln2")
    if (
        metrics.expected_calibration_error is None
        or metrics.expected_calibration_error > CALIBRATION_MAX_ECE
    ):
        reasons.append("ece_above_0.10")
    if ece_90_upper > CALIBRATION_MAX_ECE_90_UPPER:
        reasons.append("ece_90_upper_above_0.15")
    return not reasons, tuple(reasons)


def _build_artifact(
    samples: Iterable[PrematchCalibrationSample],
    calibration_cutoff: datetime,
    *,
    model_kind: str,
    availability_mode: str,
) -> PrematchCalibrationArtifact:
    cutoff = _utc(calibration_cutoff, "calibration_cutoff")
    kind = _model_kind(model_kind)
    mode = _mode(availability_mode)
    stream = _canonical_oos_stream(
        samples,
        calibration_cutoff=cutoff,
        model_kind=kind,
        availability_mode=mode,
    )
    stream_hash = _hash([row.to_payload() for row in stream])
    origin_model_hashes = tuple(sorted({row.model_hash for row in stream}))
    split = _choose_series_split(stream)
    fit = split.fit
    evaluation = split.evaluation
    fit_cutoff = max((row.result_usable_at for row in fit), default=None)
    evaluation_start = min((row.prediction_cutoff for row in evaluation), default=None)
    fit_series_ids = tuple(sorted({row.series_id for row in fit}))
    evaluation_series_ids = tuple(sorted({row.series_id for row in evaluation}))
    bootstrap_seed = _hash(
        {
            "domain": "prematch-calibration-bootstrap-seed/v1",
            "calibration_version": PREMATCH_CALIBRATION_VERSION,
            "oos_stream_hash": stream_hash,
            "fit_match_ids": [row.match_id for row in fit],
            "evaluation_match_ids": [row.match_id for row in evaluation],
        }
    )
    input_payload = {
        "domain": "prematch-calibration-input/v1",
        "calibration_version": PREMATCH_CALIBRATION_VERSION,
        "model_kind": kind,
        "availability_mode": mode,
        "calibration_cutoff": cutoff.isoformat(),
        "oos_stream_hash": stream_hash,
        "origin_model_hashes": list(origin_model_hashes),
        "fit_match_ids": [row.match_id for row in fit],
        "evaluation_match_ids": [row.match_id for row in evaluation],
        "solver": dict(CALIBRATION_SOLVER),
        "bootstrap_algorithm": CALIBRATION_BOOTSTRAP_ALGORITHM,
        "bootstrap_samples": CALIBRATION_BOOTSTRAP_SAMPLES,
        "bootstrap_confidence": CALIBRATION_BOOTSTRAP_CONFIDENCE,
        "bootstrap_seed": bootstrap_seed,
    }
    input_hash = _hash(input_payload)
    raw_metrics = (
        None
        if not evaluation
        else _metrics(evaluation, [row.raw_probability for row in evaluation])
    )
    parameters: tuple[float, float] | None = None
    calibrated_metrics: BinaryMetrics | None = None
    ece_90_upper: float | None = None
    gate_passed = False
    gate_reasons: tuple[str, ...]
    if split.reason is not None:
        status = CalibrationStatus.UNSUPPORTED
        reason = split.reason
        gate_reasons = (split.reason,)
    else:
        parameters = _fit_platt(fit)
        if parameters is None:
            status = CalibrationStatus.FAILED
            reason = "optimizer_failed"
            gate_reasons = (reason,)
        else:
            calibrated_probabilities = tuple(
                _calibrated_probability(parameters, row.raw_probability)
                for row in evaluation
            )
            calibrated_metrics = _metrics(evaluation, calibrated_probabilities)
            point_ece = calibrated_metrics.expected_calibration_error
            if point_ece is None:
                raise AssertionError("non-empty calibration evaluation has no ECE")
            ece_90_upper = _bootstrap_ece_upper(
                evaluation,
                calibrated_probabilities,
                seed=bootstrap_seed,
                point_ece=point_ece,
            )
            gate_passed, gate_reasons = _gate(
                calibrated_metrics,
                ece_90_upper,
            )
            reason = None
            if mode == AvailabilityMode.RECONSTRUCTED.value:
                status = CalibrationStatus.RECONSTRUCTED_ONLY
            elif gate_passed:
                status = CalibrationStatus.PASSED
            else:
                status = CalibrationStatus.PROVISIONAL
    partial = PrematchCalibrationArtifact(
        artifact_schema=PREMATCH_CALIBRATION_ARTIFACT_SCHEMA,
        calibration_version=PREMATCH_CALIBRATION_VERSION,
        model_kind=kind,
        availability_mode=mode,
        status=status,
        reason=reason,
        calibration_cutoff=cutoff,
        fit_cutoff=fit_cutoff,
        evaluation_start=evaluation_start,
        fit_support=len(fit),
        evaluation_support=len(evaluation),
        fit_series_ids=fit_series_ids,
        evaluation_series_ids=evaluation_series_ids,
        parameters=parameters,
        raw_metrics=raw_metrics,
        calibrated_metrics=calibrated_metrics,
        ece_90_upper=ece_90_upper,
        gate_passed=gate_passed,
        gate_reasons=gate_reasons,
        origin_model_hashes=origin_model_hashes,
        oos_stream_hash=stream_hash,
        input_hash=input_hash,
        runtime_versions=CALIBRATION_RUNTIME,
        solver=CALIBRATION_SOLVER,
        bootstrap_algorithm=CALIBRATION_BOOTSTRAP_ALGORITHM,
        bootstrap_samples=CALIBRATION_BOOTSTRAP_SAMPLES,
        bootstrap_confidence=CALIBRATION_BOOTSTRAP_CONFIDENCE,
        bootstrap_seed=bootstrap_seed,
        oos_samples=stream,
        fit_samples=fit,
        evaluation_samples=evaluation,
        calibration_hash="",
    )
    return replace(
        partial,
        calibration_hash=_hash(partial.to_payload(include_calibration_hash=False)),
    )


def build_prematch_calibration_artifact(
    samples: Iterable[PrematchCalibrationSample],
    calibration_cutoff: datetime,
    *,
    model_kind: str,
    availability_mode: str,
) -> PrematchCalibrationArtifact:
    return _build_artifact(
        samples,
        calibration_cutoff,
        model_kind=model_kind,
        availability_mode=availability_mode,
    )


def build_prematch_calibration(
    samples: Iterable[PrematchCalibrationSample],
    calibration_cutoff: datetime,
    *,
    model_kind: str,
    availability_mode: str,
) -> PrematchCalibrationArtifact:
    return build_prematch_calibration_artifact(
        samples,
        calibration_cutoff,
        model_kind=model_kind,
        availability_mode=availability_mode,
    )


_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_schema",
        "calibration_version",
        "model_kind",
        "availability_mode",
        "status",
        "reason",
        "calibration_cutoff",
        "fit_cutoff",
        "evaluation_start",
        "fit_support",
        "evaluation_support",
        "fit_series_ids",
        "evaluation_series_ids",
        "parameters",
        "raw_metrics",
        "calibrated_metrics",
        "ece_90_upper",
        "gate_passed",
        "gate_reasons",
        "origin_model_hashes",
        "oos_stream_hash",
        "input_hash",
        "runtime_versions",
        "solver",
        "bootstrap_algorithm",
        "bootstrap_samples",
        "bootstrap_confidence",
        "bootstrap_seed",
        "oos_samples",
        "fit_samples",
        "evaluation_samples",
        "calibration_hash",
    }
)
_SAMPLE_FIELDS = frozenset(
    {
        "match_id",
        "series_id",
        "event_id",
        "patch_id",
        "model_kind",
        "availability_mode",
        "prediction_cutoff",
        "result_usable_at",
        "raw_probability",
        "outcome",
        "model_hash",
        "input_snapshot_hash",
        "is_out_of_sample",
    }
)
_METRIC_FIELDS = frozenset(
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
_BIN_FIELDS = frozenset(
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


def _exact_object(
    value: object,
    fields: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object with string keys")
    actual = frozenset(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        details: list[str] = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown: {', '.join(unknown)}")
        raise ValueError(f"{label} keys do not match ({'; '.join(details)})")
    return value


def _array(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _optional_float(value: object, field: str) -> float | None:
    return None if value is None else _finite(value, field)


def _sample_from_payload(value: object) -> PrematchCalibrationSample:
    row = _exact_object(value, _SAMPLE_FIELDS, "calibration sample")
    if row["is_out_of_sample"] is not True:
        raise ValueError("calibration sample is not out of sample")
    sample = PrematchCalibrationSample(
        match_id=_positive_int(row["match_id"], "match_id"),
        series_id=_nonempty(row["series_id"], "series_id"),
        event_id=_nonempty(row["event_id"], "event_id"),
        patch_id=_nonempty(row["patch_id"], "patch_id"),
        model_kind=_model_kind(row["model_kind"]),
        availability_mode=_mode(row["availability_mode"]),
        prediction_cutoff=_parse_utc(row["prediction_cutoff"], "prediction_cutoff"),
        result_usable_at=_parse_utc(row["result_usable_at"], "result_usable_at"),
        raw_probability=_probability(row["raw_probability"], "raw_probability"),
        outcome=_binary(row["outcome"]),
        model_hash=_digest(row["model_hash"], "model_hash"),
        input_snapshot_hash=_digest(
            row["input_snapshot_hash"],
            "input_snapshot_hash",
        ),
        is_out_of_sample=True,
    )
    if canonical_json_bytes(sample.to_payload()) != canonical_json_bytes(dict(row)):
        raise ValueError("calibration sample payload is not canonical")
    return sample


def _metrics_from_payload(value: object, field: str) -> BinaryMetrics | None:
    if value is None:
        return None
    row = _exact_object(value, _METRIC_FIELDS, field)
    bins: list[CalibrationBin] = []
    for raw_bin in _array(row["calibration_bins"], f"{field}.calibration_bins"):
        bin_row = _exact_object(raw_bin, _BIN_FIELDS, "calibration bin")
        bins.append(
            CalibrationBin(
                bin_number=_positive_int(bin_row["bin_number"], "bin_number"),
                count=_positive_int(bin_row["count"], "count"),
                min_probability=_probability(
                    bin_row["min_probability"],
                    "min_probability",
                ),
                max_probability=_probability(
                    bin_row["max_probability"],
                    "max_probability",
                ),
                mean_probability=_probability(
                    bin_row["mean_probability"],
                    "mean_probability",
                ),
                event_rate=_probability(bin_row["event_rate"], "event_rate"),
                absolute_gap=_probability(bin_row["absolute_gap"], "absolute_gap"),
            )
        )
    metrics = BinaryMetrics(
        support=_nonnegative_int(row["support"], "support"),
        brier_score=_optional_float(row["brier_score"], "brier_score"),
        log_loss=_optional_float(row["log_loss"], "log_loss"),
        expected_calibration_error=_optional_float(
            row["expected_calibration_error"],
            "expected_calibration_error",
        ),
        auc=_optional_float(row["auc"], "auc"),
        accuracy=_optional_float(row["accuracy"], "accuracy"),
        classification_threshold=_probability(
            row["classification_threshold"],
            "classification_threshold",
        ),
        calibration_bins=tuple(bins),
    )
    if canonical_json_bytes(metrics.to_payload()) != canonical_json_bytes(dict(row)):
        raise ValueError(f"{field} payload is not canonical")
    return metrics


def _strict_json_object(payload_json: str) -> Mapping[str, Any]:
    if not isinstance(payload_json, str):
        raise ValueError("prematch calibration artifact JSON must be a string")

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
        raise ValueError("prematch calibration artifact JSON is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("prematch calibration artifact must be an object")
    canonical_json = canonical_json_bytes(payload).decode("utf-8")
    if not hmac.compare_digest(
        payload_json.encode("utf-8"),
        canonical_json.encode("utf-8"),
    ):
        raise ValueError("prematch calibration artifact JSON is not canonical")
    return payload


def prematch_calibration_artifact_from_payload(
    payload: Mapping[str, Any],
) -> PrematchCalibrationArtifact:
    row = _exact_object(payload, _ARTIFACT_FIELDS, "prematch calibration artifact")
    if row["artifact_schema"] != PREMATCH_CALIBRATION_ARTIFACT_SCHEMA:
        raise ValueError("unsupported prematch calibration artifact schema")
    if row["calibration_version"] != PREMATCH_CALIBRATION_VERSION:
        raise ValueError("unsupported prematch calibration version")
    status = CalibrationStatus(row["status"])
    reason = row["reason"]
    if reason is not None:
        reason = _nonempty(reason, "reason")
    parameters_raw = row["parameters"]
    parameters: tuple[float, float] | None
    if parameters_raw is None:
        parameters = None
    else:
        parameter_row = _exact_object(
            parameters_raw,
            frozenset({"a", "b"}),
            "calibration parameters",
        )
        parameters = (
            _finite(parameter_row["a"], "parameter a"),
            _finite(parameter_row["b"], "parameter b"),
        )
    runtime_raw = _exact_object(
        row["runtime_versions"],
        frozenset(name for name, _version in CALIBRATION_RUNTIME),
        "runtime_versions",
    )
    runtime = tuple(
        (name, _nonempty(runtime_raw[name], f"runtime_versions.{name}"))
        for name, _version in CALIBRATION_RUNTIME
    )
    solver_raw = _exact_object(
        row["solver"],
        frozenset(name for name, _value in CALIBRATION_SOLVER),
        "solver",
    )
    solver = tuple((name, solver_raw[name]) for name, _value in CALIBRATION_SOLVER)

    def string_tuple(value: object, field: str) -> tuple[str, ...]:
        result = tuple(_nonempty(item, field) for item in _array(value, field))
        if result != tuple(sorted(set(result))):
            raise ValueError(f"{field} must be sorted and unique")
        return result

    artifact = PrematchCalibrationArtifact(
        artifact_schema=row["artifact_schema"],
        calibration_version=row["calibration_version"],
        model_kind=_model_kind(row["model_kind"]),
        availability_mode=_mode(row["availability_mode"]),
        status=status,
        reason=reason,
        calibration_cutoff=_parse_utc(
            row["calibration_cutoff"],
            "calibration_cutoff",
        ),
        fit_cutoff=(
            None
            if row["fit_cutoff"] is None
            else _parse_utc(row["fit_cutoff"], "fit_cutoff")
        ),
        evaluation_start=(
            None
            if row["evaluation_start"] is None
            else _parse_utc(row["evaluation_start"], "evaluation_start")
        ),
        fit_support=_nonnegative_int(row["fit_support"], "fit_support"),
        evaluation_support=_nonnegative_int(
            row["evaluation_support"],
            "evaluation_support",
        ),
        fit_series_ids=string_tuple(row["fit_series_ids"], "fit_series_ids"),
        evaluation_series_ids=string_tuple(
            row["evaluation_series_ids"],
            "evaluation_series_ids",
        ),
        parameters=parameters,
        raw_metrics=_metrics_from_payload(row["raw_metrics"], "raw_metrics"),
        calibrated_metrics=_metrics_from_payload(
            row["calibrated_metrics"],
            "calibrated_metrics",
        ),
        ece_90_upper=_optional_float(row["ece_90_upper"], "ece_90_upper"),
        gate_passed=_boolean(row["gate_passed"], "gate_passed"),
        gate_reasons=tuple(
            _nonempty(item, "gate_reasons")
            for item in _array(row["gate_reasons"], "gate_reasons")
        ),
        origin_model_hashes=tuple(
            _digest(item, "origin_model_hash")
            for item in _array(row["origin_model_hashes"], "origin_model_hashes")
        ),
        oos_stream_hash=_digest(row["oos_stream_hash"], "oos_stream_hash"),
        input_hash=_digest(row["input_hash"], "input_hash"),
        runtime_versions=runtime,
        solver=solver,
        bootstrap_algorithm=_nonempty(
            row["bootstrap_algorithm"],
            "bootstrap_algorithm",
        ),
        bootstrap_samples=_positive_int(
            row["bootstrap_samples"],
            "bootstrap_samples",
        ),
        bootstrap_confidence=_probability(
            row["bootstrap_confidence"],
            "bootstrap_confidence",
        ),
        bootstrap_seed=_digest(row["bootstrap_seed"], "bootstrap_seed"),
        oos_samples=tuple(
            _sample_from_payload(item)
            for item in _array(row["oos_samples"], "oos_samples")
        ),
        fit_samples=tuple(
            _sample_from_payload(item)
            for item in _array(row["fit_samples"], "fit_samples")
        ),
        evaluation_samples=tuple(
            _sample_from_payload(item)
            for item in _array(row["evaluation_samples"], "evaluation_samples")
        ),
        calibration_hash=_digest(row["calibration_hash"], "calibration_hash"),
    )
    replay_prematch_calibration_artifact(artifact)
    if canonical_json_bytes(artifact.to_payload()) != canonical_json_bytes(dict(row)):
        raise ValueError("prematch calibration artifact payload is not canonical")
    return artifact


def replay_prematch_calibration_artifact(
    artifact: PrematchCalibrationArtifact,
) -> PrematchCalibrationArtifact:
    if not isinstance(artifact, PrematchCalibrationArtifact):
        raise ValueError("artifact must be a PrematchCalibrationArtifact")
    if artifact.artifact_schema != PREMATCH_CALIBRATION_ARTIFACT_SCHEMA:
        raise ValueError("unsupported prematch calibration artifact schema")
    if artifact.calibration_version != PREMATCH_CALIBRATION_VERSION:
        raise ValueError("unsupported prematch calibration version")
    if artifact.runtime_versions != CALIBRATION_RUNTIME:
        raise ValueError("prematch calibration runtime identity does not match")
    if artifact.solver != CALIBRATION_SOLVER:
        raise ValueError("prematch calibration solver identity does not match")
    if (
        artifact.bootstrap_algorithm != CALIBRATION_BOOTSTRAP_ALGORITHM
        or artifact.bootstrap_samples != CALIBRATION_BOOTSTRAP_SAMPLES
        or artifact.bootstrap_confidence != CALIBRATION_BOOTSTRAP_CONFIDENCE
    ):
        raise ValueError("prematch calibration bootstrap identity does not match")
    expected_hash = _hash(artifact.to_payload(include_calibration_hash=False))
    if not hmac.compare_digest(expected_hash, artifact.calibration_hash):
        raise ValueError("prematch calibration hash does not recompute")
    replayed = _build_artifact(
        artifact.oos_samples,
        artifact.calibration_cutoff,
        model_kind=artifact.model_kind,
        availability_mode=artifact.availability_mode,
    )
    actual = canonical_json_bytes(artifact.to_payload(include_calibration_hash=False))
    expected = canonical_json_bytes(replayed.to_payload(include_calibration_hash=False))
    if not hmac.compare_digest(actual, expected):
        raise ValueError("prematch calibration does not replay from its OOS stream")
    return replayed


def load_prematch_calibration_artifact_json(
    payload_json: str,
) -> PrematchCalibrationArtifact:
    return prematch_calibration_artifact_from_payload(_strict_json_object(payload_json))


def load_calibration_artifact_json(payload_json: str) -> PrematchCalibrationArtifact:
    return load_prematch_calibration_artifact_json(payload_json)


def _apply_verified_prematch_calibration(
    artifact: PrematchCalibrationArtifact,
    raw_probability: float,
    *,
    prediction_cutoff: datetime,
    availability_mode: str,
    model_hash: str,
    input_snapshot_hash: str,
) -> PrematchCalibrationApplication:
    raw = _probability(raw_probability, "raw_probability")
    cutoff = _utc(prediction_cutoff, "prediction_cutoff")
    mode = _mode(availability_mode)
    if mode != artifact.availability_mode:
        raise ValueError("calibrator and prediction availability modes disagree")
    origin_model_hash = _digest(model_hash, "model_hash")
    snapshot_hash = _digest(input_snapshot_hash, "input_snapshot_hash")
    if artifact.calibration_cutoff >= cutoff:
        raise ValueError("calibration artifact was not usable before prediction cutoff")
    if artifact.parameters is None:
        return PrematchCalibrationApplication(
            status=artifact.status,
            reason=artifact.reason,
            model_kind=artifact.model_kind,
            availability_mode=mode,
            prediction_cutoff=cutoff,
            raw_probability=raw,
            calibrated_probability=None,
            model_hash=origin_model_hash,
            input_snapshot_hash=snapshot_hash,
            calibration_hash=artifact.calibration_hash,
        )
    if artifact.fit_cutoff is None:
        raise ValueError("calibrator has no causal fit cutoff")
    return PrematchCalibrationApplication(
        status=artifact.status,
        reason=artifact.reason,
        model_kind=artifact.model_kind,
        availability_mode=mode,
        prediction_cutoff=cutoff,
        raw_probability=raw,
        calibrated_probability=_calibrated_probability(artifact.parameters, raw),
        model_hash=origin_model_hash,
        input_snapshot_hash=snapshot_hash,
        calibration_hash=artifact.calibration_hash,
    )


def apply_prematch_calibration(
    artifact: PrematchCalibrationArtifact,
    raw_probability: float,
    *,
    prediction_cutoff: datetime,
    availability_mode: str,
    model_hash: str,
    input_snapshot_hash: str,
) -> PrematchCalibrationApplication:
    replay_prematch_calibration_artifact(artifact)
    return _apply_verified_prematch_calibration(
        artifact,
        raw_probability,
        prediction_cutoff=prediction_cutoff,
        availability_mode=availability_mode,
        model_hash=model_hash,
        input_snapshot_hash=input_snapshot_hash,
    )


def load_and_apply_prematch_calibration_json(
    payload_json: str,
    raw_probability: float,
    *,
    prediction_cutoff: datetime,
    availability_mode: str,
    model_hash: str,
    input_snapshot_hash: str,
) -> PrematchCalibrationApplication:
    artifact = load_prematch_calibration_artifact_json(payload_json)
    return _apply_verified_prematch_calibration(
        artifact,
        raw_probability,
        prediction_cutoff=prediction_cutoff,
        availability_mode=availability_mode,
        model_hash=model_hash,
        input_snapshot_hash=input_snapshot_hash,
    )


def apply_calibration(
    artifact: PrematchCalibrationArtifact,
    raw_probability: float,
    *,
    prediction_cutoff: datetime,
    availability_mode: str,
    model_hash: str,
    input_snapshot_hash: str,
) -> PrematchCalibrationApplication:
    return apply_prematch_calibration(
        artifact,
        raw_probability,
        prediction_cutoff=prediction_cutoff,
        availability_mode=availability_mode,
        model_hash=model_hash,
        input_snapshot_hash=input_snapshot_hash,
    )


__all__ = [
    "CALIBRATION_BOOTSTRAP_ALGORITHM",
    "CALIBRATION_BOOTSTRAP_CONFIDENCE",
    "CALIBRATION_BOOTSTRAP_SAMPLES",
    "CALIBRATION_ECE_BINS",
    "CALIBRATION_MIN_EVALUATION_SUPPORT",
    "CALIBRATION_MIN_FIT_SUPPORT",
    "CALIBRATION_RUNTIME",
    "PREMATCH_CALIBRATION_ARTIFACT_SCHEMA",
    "PREMATCH_CALIBRATION_VERSION",
    "CalibrationStatus",
    "PrematchCalibrationApplication",
    "PrematchCalibrationArtifact",
    "PrematchCalibrationSample",
    "apply_calibration",
    "apply_prematch_calibration",
    "build_prematch_calibration",
    "build_prematch_calibration_artifact",
    "load_and_apply_prematch_calibration_json",
    "load_calibration_artifact_json",
    "load_prematch_calibration_artifact_json",
    "prematch_calibration_artifact_from_payload",
    "replay_prematch_calibration_artifact",
]
