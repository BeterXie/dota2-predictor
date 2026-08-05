"""PostgreSQL persistence for replayable prematch model evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from database.session import DatabaseRow, PostgresSession

from .prematch_calibration import (
    PrematchCalibrationArtifact,
    load_and_apply_prematch_calibration_json,
    load_prematch_calibration_artifact_json,
)
from .prematch_artifacts import (
    canonical_json_bytes,
    load_prematch_model_artifact_json,
)
from .prematch_features import PrematchFeatureSnapshot, verify_prematch_feature_snapshot
from .prematch_model import (
    ModelStatus,
    PredictionStatus,
    PrematchModelArtifact,
    PrematchPrediction,
    predict_prematch,
)


UTC = timezone.utc
PREMATCH_VALIDATION_VERSION = "prematch-input-lineage-v1"
_MODEL_KINDS = frozenset(
    {"team_only", "team_plus_draft", "team_plus_rosh", "team_plus_draft_rosh"}
)
_AVAILABILITY_MODES = frozenset({"reconstructed_walk_forward", "prospective"})
_CALIBRATION_STATUSES = frozenset(
    {
        "unsupported",
        "failed",
        "provisional",
        "reconstructed_only",
        "shadow_collecting",
        "passed",
    }
)
_PREDICTION_STATUSES = frozenset(
    {"predicted", "insufficient_evidence", "failed", "settled"}
)
T = TypeVar("T")


def _canonical_json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _utc(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _optional_sha256(value: object, field: str) -> str | None:
    return None if value is None else _sha256(value, field)


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _finite(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{field} must be finite")
    return float(value)


def _optional_finite(value: object, field: str) -> float | None:
    return None if value is None else _finite(value, field)


def _probability(value: object, field: str, *, strict: bool = False) -> float:
    result = _finite(value, field)
    valid = 0.0 < result < 1.0 if strict else 0.0 <= result <= 1.0
    if not valid:
        qualifier = "strictly " if strict else ""
        raise ValueError(f"{field} must be {qualifier}between zero and one")
    return result


def _optional_probability(value: object, field: str) -> float | None:
    return None if value is None else _probability(value, field)


def _strict_sigmoid(value: float) -> float:
    if value >= 0.0:
        probability = 1.0 / (1.0 + math.exp(-value))
    else:
        exponential = math.exp(value)
        probability = exponential / (1.0 + exponential)
    epsilon = 1e-15
    return min(max(probability, epsilon), 1.0 - epsilon)


def _canonical_object_json(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be canonical JSON")
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"{field} must be canonical JSON") from error
    if not isinstance(parsed, dict) or _canonical_json(parsed) != value:
        raise ValueError(f"{field} must be a canonical JSON object")
    return value


def _optional_canonical_object_json(value: object, field: str) -> str | None:
    return None if value is None else _canonical_object_json(value, field)


def prematch_artifact_fingerprint(
    *,
    model_hash: str,
    calibration_hash: str | None,
) -> str:
    return _hash(
        {
            "domain": "prematch-artifact-fingerprint/v1",
            "model_hash": _sha256(model_hash, "model_hash"),
            "calibration_hash": _optional_sha256(
                calibration_hash,
                "calibration_hash",
            ),
        }
    )


def prematch_dependency_revision_is_current(
    connection: PostgresSession,
    *,
    dependency_revision: int,
    prediction_cutoff: datetime,
) -> bool:
    revision = _positive_int(dependency_revision, "dependency_revision")
    cutoff = _utc(prediction_cutoff, "prediction_cutoff")
    row = connection.execute(
        """SELECT prematch_lineage_revision_is_current(?, ?) AS is_current""",
        (revision, cutoff.isoformat()),
    ).fetchone()
    return row is not None and bool(row["is_current"])


def require_prematch_dependency_revision_current(
    connection: PostgresSession,
    *,
    dependency_revision: int,
    evaluation_cutoff: datetime,
) -> None:
    revision = _positive_int(dependency_revision, "dependency_revision")
    cutoff = _utc(evaluation_cutoff, "evaluation_cutoff")
    authority = connection.execute(
        """SELECT dependency_revision
             FROM prematch_lineage_revisions
            WHERE singleton=1
            FOR UPDATE"""
    ).fetchone()
    if authority is None or int(authority["dependency_revision"]) < revision:
        raise RuntimeError("prematch dependency revision authority is unavailable")
    if not prematch_dependency_revision_is_current(
        connection,
        dependency_revision=revision,
        prediction_cutoff=cutoff,
    ):
        raise RuntimeError("prematch dependencies changed while result was rebuilding")


@dataclass(frozen=True)
class PrematchModelRunRecord:
    run_id: str
    model_version: str
    artifact_version: str
    model_kind: str
    availability_mode: str
    training_cutoff: datetime
    feature_schema_hash: str
    training_input_hash: str
    model_hash: str
    artifact_json: str
    metrics_json: str | None
    status: str

    def __post_init__(self) -> None:
        run_id = _sha256(self.run_id, "run_id")
        model_hash = _sha256(self.model_hash, "model_hash")
        if run_id != model_hash:
            raise ValueError("prematch run_id must equal model_hash")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "model_hash", model_hash)
        _nonempty(self.model_version, "model_version")
        _nonempty(self.artifact_version, "artifact_version")
        if self.model_kind not in _MODEL_KINDS:
            raise ValueError("unsupported prematch model kind")
        if self.availability_mode not in _AVAILABILITY_MODES:
            raise ValueError("unsupported prematch availability mode")
        object.__setattr__(
            self,
            "training_cutoff",
            _utc(self.training_cutoff, "training_cutoff"),
        )
        object.__setattr__(
            self,
            "feature_schema_hash",
            _sha256(self.feature_schema_hash, "feature_schema_hash"),
        )
        object.__setattr__(
            self,
            "training_input_hash",
            _sha256(self.training_input_hash, "training_input_hash"),
        )
        artifact_json = _canonical_object_json(self.artifact_json, "artifact_json")
        object.__setattr__(self, "artifact_json", artifact_json)
        object.__setattr__(
            self,
            "metrics_json",
            _optional_canonical_object_json(self.metrics_json, "metrics_json"),
        )
        if self.status not in {status.value for status in ModelStatus}:
            raise ValueError("unsupported prematch model status")
        artifact = load_prematch_model_artifact_json(artifact_json)
        expected = (
            artifact.model_version,
            artifact.artifact_version,
            artifact.model_kind,
            artifact.availability_mode,
            artifact.training_cutoff,
            artifact.feature_schema_hash,
            artifact.training_input_hash,
            artifact.model_hash,
            artifact.status.value,
        )
        actual = (
            self.model_version,
            self.artifact_version,
            self.model_kind,
            self.availability_mode,
            self.training_cutoff,
            self.feature_schema_hash,
            self.training_input_hash,
            self.model_hash,
            self.status,
        )
        if actual != expected:
            raise ValueError("prematch model record disagrees with replayed artifact")

    def stable_columns(self) -> tuple[object, ...]:
        return (
            self.model_version,
            self.artifact_version,
            self.model_kind,
            self.availability_mode,
            self.training_cutoff.isoformat(),
            self.feature_schema_hash,
            self.training_input_hash,
            self.model_hash,
            self.artifact_json,
            self.metrics_json,
            self.status,
        )


@dataclass(frozen=True)
class PrematchCalibrationRecord:
    calibration_key: str
    model_kind: str
    model_hash: str
    calibration_version: str
    fit_cutoff: datetime | None
    evaluation_cutoff: datetime
    fit_support: int
    evaluation_support: int
    parameters_json: str | None
    metrics_json: str
    input_hash: str
    calibration_hash: str
    artifact_json: str
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "calibration_key",
            _sha256(self.calibration_key, "calibration_key"),
        )
        if self.model_kind not in _MODEL_KINDS:
            raise ValueError("unsupported calibration model kind")
        object.__setattr__(self, "model_hash", _sha256(self.model_hash, "model_hash"))
        _nonempty(self.calibration_version, "calibration_version")
        fit_cutoff = (
            None if self.fit_cutoff is None else _utc(self.fit_cutoff, "fit_cutoff")
        )
        evaluation_cutoff = _utc(self.evaluation_cutoff, "evaluation_cutoff")
        if fit_cutoff is not None and fit_cutoff > evaluation_cutoff:
            raise ValueError("calibration fit cutoff follows evaluation cutoff")
        object.__setattr__(self, "fit_cutoff", fit_cutoff)
        object.__setattr__(self, "evaluation_cutoff", evaluation_cutoff)
        _nonnegative_int(self.fit_support, "fit_support")
        _nonnegative_int(self.evaluation_support, "evaluation_support")
        object.__setattr__(
            self,
            "parameters_json",
            _optional_canonical_object_json(
                self.parameters_json,
                "parameters_json",
            ),
        )
        for field in ("metrics_json", "artifact_json"):
            object.__setattr__(
                self, field, _canonical_object_json(getattr(self, field), field)
            )
        object.__setattr__(self, "input_hash", _sha256(self.input_hash, "input_hash"))
        object.__setattr__(
            self,
            "calibration_hash",
            _sha256(self.calibration_hash, "calibration_hash"),
        )
        if self.status not in _CALIBRATION_STATUSES:
            raise ValueError("unsupported calibration status")
        if fit_cutoff is None and self.status != "unsupported":
            raise ValueError("only unsupported calibration may omit fit_cutoff")
        artifact = load_prematch_calibration_artifact_json(self.artifact_json)
        expected_parameters = (
            None
            if artifact.parameters is None
            else _canonical_json(
                {"a": artifact.parameters[0], "b": artifact.parameters[1]}
            )
        )
        expected_metrics = _canonical_json(
            {
                "raw_metrics": (
                    None
                    if artifact.raw_metrics is None
                    else artifact.raw_metrics.to_payload()
                ),
                "calibrated_metrics": (
                    None
                    if artifact.calibrated_metrics is None
                    else artifact.calibrated_metrics.to_payload()
                ),
                "ece_90_upper": artifact.ece_90_upper,
                "gate_passed": artifact.gate_passed,
                "gate_reasons": list(artifact.gate_reasons),
            }
        )
        expected = (
            artifact.model_kind,
            artifact.calibration_version,
            artifact.fit_cutoff,
            artifact.calibration_cutoff,
            artifact.fit_support,
            artifact.evaluation_support,
            expected_parameters,
            expected_metrics,
            artifact.input_hash,
            artifact.calibration_hash,
            artifact.status.value,
        )
        actual = (
            self.model_kind,
            self.calibration_version,
            self.fit_cutoff,
            self.evaluation_cutoff,
            self.fit_support,
            self.evaluation_support,
            self.parameters_json,
            self.metrics_json,
            self.input_hash,
            self.calibration_hash,
            self.status,
        )
        if actual != expected:
            raise ValueError("calibration record disagrees with replayed artifact")
        expected_key = _hash(
            {
                "domain": "prematch-calibration-key/v1",
                "model_hash": self.model_hash,
                "calibration_hash": self.calibration_hash,
                "evaluation_cutoff": self.evaluation_cutoff.isoformat(),
            }
        )
        if not hmac.compare_digest(self.calibration_key, expected_key):
            raise ValueError("calibration key does not recompute")

    def stable_columns(self) -> tuple[object, ...]:
        return (
            self.model_kind,
            self.model_hash,
            self.calibration_version,
            None if self.fit_cutoff is None else self.fit_cutoff.isoformat(),
            self.evaluation_cutoff.isoformat(),
            self.fit_support,
            self.evaluation_support,
            self.parameters_json,
            self.metrics_json,
            self.input_hash,
            self.calibration_hash,
            self.artifact_json,
            self.status,
        )


@dataclass(frozen=True)
class PrematchPredictionRecord:
    run_id: str
    match_id: int
    prediction_cutoff: datetime
    cutoff_source: str
    input_snapshot_hash: str
    artifact_fingerprint: str
    dependency_fingerprint: str
    dependency_revision: int
    calibration_hash: str | None
    team_base_probability: float
    raw_probability: float | None
    calibrated_probability: float | None
    parameter_uncertainty: float | None
    draft_logit_delta: float | None
    rosh_logit_delta: float | None
    cluster_logit_delta: float | None
    total_adjustment: float | None
    coverage: float
    support: int
    prediction_json: str
    eventual_radiant_win: bool | None
    result_usable_at: datetime | None
    settled_at: datetime | None
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _sha256(self.run_id, "run_id"))
        _positive_int(self.match_id, "match_id")
        object.__setattr__(
            self,
            "prediction_cutoff",
            _utc(self.prediction_cutoff, "prediction_cutoff"),
        )
        _nonempty(self.cutoff_source, "cutoff_source")
        for field in (
            "input_snapshot_hash",
            "artifact_fingerprint",
            "dependency_fingerprint",
        ):
            object.__setattr__(self, field, _sha256(getattr(self, field), field))
        _positive_int(self.dependency_revision, "dependency_revision")
        object.__setattr__(
            self,
            "calibration_hash",
            _optional_sha256(self.calibration_hash, "calibration_hash"),
        )
        object.__setattr__(
            self,
            "team_base_probability",
            _probability(
                self.team_base_probability, "team_base_probability", strict=True
            ),
        )
        for field in ("raw_probability", "calibrated_probability"):
            object.__setattr__(
                self,
                field,
                _optional_probability(getattr(self, field), field),
            )
        for field in (
            "parameter_uncertainty",
            "draft_logit_delta",
            "rosh_logit_delta",
            "cluster_logit_delta",
            "total_adjustment",
        ):
            value = _optional_finite(getattr(self, field), field)
            if field == "parameter_uncertainty" and value is not None and value < 0.0:
                raise ValueError("parameter_uncertainty must be nonnegative")
            object.__setattr__(self, field, value)
        object.__setattr__(self, "coverage", _probability(self.coverage, "coverage"))
        _nonnegative_int(self.support, "support")
        prediction_json = _canonical_object_json(
            self.prediction_json, "prediction_json"
        )
        object.__setattr__(self, "prediction_json", prediction_json)
        if self.eventual_radiant_win is not None and not isinstance(
            self.eventual_radiant_win,
            bool,
        ):
            raise ValueError("eventual_radiant_win must be boolean or None")
        for field in ("result_usable_at", "settled_at"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _utc(value, field))
        if self.status not in _PREDICTION_STATUSES:
            raise ValueError("unsupported prematch prediction status")
        settled = self.status == "settled"
        if settled != all(
            value is not None
            for value in (
                self.eventual_radiant_win,
                self.result_usable_at,
                self.settled_at,
            )
        ):
            raise ValueError("prematch settlement fields are inconsistent")
        if self.result_usable_at is not None and self.settled_at is not None:
            if self.result_usable_at > self.settled_at:
                raise ValueError("result_usable_at follows settled_at")
        predicted = self.status in {"predicted", "settled"}
        if predicted != (self.raw_probability is not None):
            raise ValueError("prematch probability disagrees with status")
        if (self.calibration_hash is None) != (self.calibrated_probability is None):
            raise ValueError("calibrated probability lacks exact calibration identity")
        payload = json.loads(prediction_json)
        payload_status = payload.get("status")
        expected_payload_status = "predicted" if settled else self.status
        claims = (
            payload_status,
            payload.get("model_hash"),
            payload.get("input_snapshot_hash"),
            payload.get("support"),
            payload.get("raw_probability"),
            payload.get("parameter_uncertainty"),
            payload.get("draft_logit_delta"),
            payload.get("rosh_logit_delta"),
            payload.get("cluster_logit_delta"),
            payload.get("total_adjustment"),
        )
        expected = (
            expected_payload_status,
            self.run_id,
            self.input_snapshot_hash,
            self.support,
            self.raw_probability,
            self.parameter_uncertainty,
            self.draft_logit_delta,
            self.rosh_logit_delta,
            self.cluster_logit_delta,
            self.total_adjustment,
        )
        team_base_logit = payload.get("team_base_logit")
        if (
            claims != expected
            or isinstance(team_base_logit, bool)
            or not isinstance(team_base_logit, (int, float))
            or not math.isfinite(float(team_base_logit))
            or not math.isclose(
                self.team_base_probability,
                _strict_sigmoid(float(team_base_logit)),
                rel_tol=0.0,
                abs_tol=1e-15,
            )
        ):
            raise ValueError(
                "prematch prediction record disagrees with prediction artifact"
            )

    def immutable_columns(self) -> tuple[object, ...]:
        return (
            self.match_id,
            self.prediction_cutoff.isoformat(),
            self.cutoff_source,
            self.input_snapshot_hash,
            self.artifact_fingerprint,
            self.dependency_fingerprint,
            self.dependency_revision,
            self.calibration_hash,
            self.team_base_probability,
            self.raw_probability,
            self.calibrated_probability,
            self.parameter_uncertainty,
            self.draft_logit_delta,
            self.rosh_logit_delta,
            self.cluster_logit_delta,
            self.total_adjustment,
            self.coverage,
            self.support,
            self.prediction_json,
        )

    def stable_columns(self) -> tuple[object, ...]:
        return (
            *self.immutable_columns(),
            None
            if self.eventual_radiant_win is None
            else int(self.eventual_radiant_win),
            None
            if self.result_usable_at is None
            else self.result_usable_at.isoformat(),
            None if self.settled_at is None else self.settled_at.isoformat(),
            self.status,
        )


@dataclass(frozen=True)
class PrematchValidationRecord:
    run_id: str
    match_id: int
    input_snapshot_hash: str
    artifact_fingerprint: str
    dependency_fingerprint: str
    dependency_revision: int
    validation_version: str
    validated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _sha256(self.run_id, "run_id"))
        _positive_int(self.match_id, "match_id")
        for field in (
            "input_snapshot_hash",
            "artifact_fingerprint",
            "dependency_fingerprint",
        ):
            object.__setattr__(self, field, _sha256(getattr(self, field), field))
        _positive_int(self.dependency_revision, "dependency_revision")
        if self.validation_version != PREMATCH_VALIDATION_VERSION:
            raise ValueError("unsupported prematch validation version")
        object.__setattr__(
            self,
            "validated_at",
            _utc(self.validated_at, "validated_at"),
        )

    def stable_columns(self) -> tuple[object, ...]:
        return (
            self.input_snapshot_hash,
            self.artifact_fingerprint,
            self.dependency_fingerprint,
            self.dependency_revision,
            self.validation_version,
            self.validated_at.isoformat(),
        )


@dataclass(frozen=True)
class PrematchPersistenceCounts:
    inserted_model_runs: int = 0
    unchanged_model_runs: int = 0
    inserted_calibrations: int = 0
    unchanged_calibrations: int = 0
    inserted_predictions: int = 0
    unchanged_predictions: int = 0
    inserted_validations: int = 0
    unchanged_validations: int = 0


@dataclass(frozen=True)
class PrematchSettlementResult:
    updated: bool
    unchanged: bool


def build_prematch_model_run_record(
    model: PrematchModelArtifact,
    *,
    metrics: Mapping[str, Any] | None = None,
) -> PrematchModelRunRecord:
    if not isinstance(model, PrematchModelArtifact):
        raise ValueError("model must be a PrematchModelArtifact")
    return PrematchModelRunRecord(
        run_id=model.model_hash,
        model_version=model.model_version,
        artifact_version=model.artifact_version,
        model_kind=model.model_kind,
        availability_mode=model.availability_mode,
        training_cutoff=model.training_cutoff,
        feature_schema_hash=model.feature_schema_hash,
        training_input_hash=model.training_input_hash,
        model_hash=model.model_hash,
        artifact_json=_canonical_json(model.to_payload()),
        metrics_json=None if metrics is None else _canonical_json(dict(metrics)),
        status=model.status.value,
    )


def build_prematch_calibration_record(
    artifact: PrematchCalibrationArtifact,
    *,
    model_hash: str,
) -> PrematchCalibrationRecord:
    if not isinstance(artifact, PrematchCalibrationArtifact):
        raise ValueError("artifact must be a PrematchCalibrationArtifact")
    identity = _sha256(model_hash, "model_hash")
    calibration_key = _hash(
        {
            "domain": "prematch-calibration-key/v1",
            "model_hash": identity,
            "calibration_hash": artifact.calibration_hash,
            "evaluation_cutoff": artifact.calibration_cutoff.isoformat(),
        }
    )
    parameters_json = (
        None
        if artifact.parameters is None
        else _canonical_json({"a": artifact.parameters[0], "b": artifact.parameters[1]})
    )
    metrics_json = _canonical_json(
        {
            "raw_metrics": (
                None
                if artifact.raw_metrics is None
                else artifact.raw_metrics.to_payload()
            ),
            "calibrated_metrics": (
                None
                if artifact.calibrated_metrics is None
                else artifact.calibrated_metrics.to_payload()
            ),
            "ece_90_upper": artifact.ece_90_upper,
            "gate_passed": artifact.gate_passed,
            "gate_reasons": list(artifact.gate_reasons),
        }
    )
    return PrematchCalibrationRecord(
        calibration_key=calibration_key,
        model_kind=artifact.model_kind,
        model_hash=identity,
        calibration_version=artifact.calibration_version,
        fit_cutoff=artifact.fit_cutoff,
        evaluation_cutoff=artifact.calibration_cutoff,
        fit_support=artifact.fit_support,
        evaluation_support=artifact.evaluation_support,
        parameters_json=parameters_json,
        metrics_json=metrics_json,
        input_hash=artifact.input_hash,
        calibration_hash=artifact.calibration_hash,
        artifact_json=_canonical_json(artifact.to_payload()),
        status=artifact.status.value,
    )


def _prediction_payload(prediction: PrematchPrediction) -> str:
    return _canonical_json(prediction.to_payload())


def prematch_dependency_fingerprint(snapshot: PrematchFeatureSnapshot) -> str:
    verify_prematch_feature_snapshot(snapshot)
    return _hash(
        {
            "domain": "prematch-dependency-fingerprint/v1",
            "availability_mode": snapshot.availability_mode,
            "match_id": snapshot.match_id,
            "prediction_cutoff": snapshot.prediction_cutoff.isoformat(),
            "feature_version": snapshot.feature_version,
            "team_rating": {
                "run_id": snapshot.team_rating_run_id,
                "artifact_hash": snapshot.team_rating_artifact_hash,
                "prediction_input_hash": (snapshot.team_rating_prediction_input_hash),
                "combined_training_input_hash": (
                    snapshot.team_rating_combined_training_input_hash
                ),
            },
            "draft_residual": {
                "input_hash": snapshot.draft_residual_input_hash,
                "authority_fingerprint": (
                    snapshot.draft_residual_authority_fingerprint
                ),
                "team_rating_input_hash": (
                    snapshot.draft_residual_team_rating_input_hash
                ),
                "feature_schema_hash": (snapshot.draft_residual_feature_schema_hash),
                "model_schema_hash": snapshot.draft_residual_model_schema_hash,
            },
            "rosh": {
                "status": snapshot.rosh_status,
                "input_hash": snapshot.rosh_input_hash,
                "run_id": snapshot.rosh_run_id,
                "evidence_hash": snapshot.rosh_evidence_hash,
                "formula_version": snapshot.rosh_formula_version,
                "profile_hash": snapshot.rosh_profile_hash,
                "result_hash": snapshot.rosh_result_hash,
                "model_schema_hash": snapshot.rosh_model_schema_hash,
            },
            "prematch_input_hash": snapshot.input_hash,
        }
    )


def build_prematch_prediction_record(
    model: PrematchModelArtifact,
    snapshot: PrematchFeatureSnapshot,
    *,
    cutoff_source: str,
    dependency_fingerprint: str | None = None,
    dependency_revision: int,
    calibration: PrematchCalibrationRecord | None = None,
    top_n: int = 5,
) -> PrematchPredictionRecord:
    verify_prematch_feature_snapshot(snapshot)
    prediction = predict_prematch(model, snapshot, top_n=top_n)
    expected_dependency = prematch_dependency_fingerprint(snapshot)
    if dependency_fingerprint is not None and not hmac.compare_digest(
        _sha256(dependency_fingerprint, "dependency_fingerprint"),
        expected_dependency,
    ):
        raise ValueError("prematch dependency fingerprint does not recompute")
    if calibration is None:
        calibration_hash = None
        calibrated_probability = None
    else:
        if calibration.model_hash != model.model_hash:
            raise ValueError("calibration artifact belongs to another model")
        if calibration.status in {"unsupported", "failed"}:
            raise ValueError(
                "unusable calibration artifact cannot calibrate a prediction"
            )
        if prediction.raw_probability is None:
            raise ValueError("insufficient prediction cannot be calibrated")
        application = load_and_apply_prematch_calibration_json(
            calibration.artifact_json,
            prediction.raw_probability,
            prediction_cutoff=snapshot.prediction_cutoff,
            availability_mode=snapshot.availability_mode,
            model_hash=model.model_hash,
            input_snapshot_hash=snapshot.input_hash,
        )
        if application.calibrated_probability is None:
            raise ValueError("calibration artifact produced no calibrated probability")
        calibration_hash = calibration.calibration_hash
        calibrated_probability = application.calibrated_probability
    status = (
        "predicted"
        if prediction.status is PredictionStatus.PREDICTED
        else "insufficient_evidence"
    )
    team_base_probability = _strict_sigmoid(snapshot.team_base_logit)
    return PrematchPredictionRecord(
        run_id=model.model_hash,
        match_id=snapshot.match_id,
        prediction_cutoff=snapshot.prediction_cutoff,
        cutoff_source=cutoff_source,
        input_snapshot_hash=snapshot.input_hash,
        artifact_fingerprint=prematch_artifact_fingerprint(
            model_hash=model.model_hash,
            calibration_hash=calibration_hash,
        ),
        dependency_fingerprint=expected_dependency,
        dependency_revision=dependency_revision,
        calibration_hash=calibration_hash,
        team_base_probability=team_base_probability,
        raw_probability=prediction.raw_probability,
        calibrated_probability=calibrated_probability,
        parameter_uncertainty=prediction.parameter_uncertainty,
        draft_logit_delta=prediction.draft_logit_delta,
        rosh_logit_delta=prediction.rosh_logit_delta,
        cluster_logit_delta=prediction.cluster_logit_delta,
        total_adjustment=prediction.total_adjustment,
        coverage=snapshot.coverage,
        support=prediction.support,
        prediction_json=_prediction_payload(prediction),
        eventual_radiant_win=None,
        result_usable_at=None,
        settled_at=None,
        status=status,
    )


def build_prematch_validation_record(
    model_run: PrematchModelRunRecord,
    prediction: PrematchPredictionRecord,
    *,
    validated_at: datetime,
) -> PrematchValidationRecord:
    # The record constructors replay embedded artifacts; the formal caller is
    # responsible for rebuilding the external feature authority before this step.
    if model_run.run_id != prediction.run_id:
        raise ValueError("validation model and prediction run IDs disagree")
    expected_artifact = prematch_artifact_fingerprint(
        model_hash=model_run.model_hash,
        calibration_hash=prediction.calibration_hash,
    )
    if prediction.artifact_fingerprint != expected_artifact:
        raise ValueError("prediction artifact fingerprint does not recompute")
    return PrematchValidationRecord(
        run_id=prediction.run_id,
        match_id=prediction.match_id,
        input_snapshot_hash=prediction.input_snapshot_hash,
        artifact_fingerprint=prediction.artifact_fingerprint,
        dependency_fingerprint=prediction.dependency_fingerprint,
        dependency_revision=prediction.dependency_revision,
        validation_version=PREMATCH_VALIDATION_VERSION,
        validated_at=validated_at,
    )


def _row_values(row: DatabaseRow | None) -> tuple[object, ...] | None:
    return None if row is None else tuple(row)


def _require_exact_row(
    row: DatabaseRow | None,
    expected: tuple[object, ...],
    message: str,
) -> None:
    if _row_values(row) != expected:
        raise ValueError(message)


def _normalize_records(
    records: Sequence[T],
    *,
    key: Callable[[T], object],
    stable: Callable[[T], tuple[object, ...]],
    conflict: Callable[[T], str],
) -> tuple[T, ...]:
    by_key: dict[object, T] = {}
    for record in records:
        identity = key(record)
        existing = by_key.get(identity)
        if existing is not None and stable(existing) != stable(record):
            raise ValueError(conflict(record))
        by_key[identity] = record
    return tuple(by_key[identity] for identity in sorted(by_key, key=str))


_PREDICTION_REVISION_INDEX = 6


def _stored_prediction_revision(
    connection: PostgresSession,
    stored: DatabaseRow | None,
    expected: PrematchPredictionRecord,
) -> int:
    actual = _row_values(stored)
    conflict = (
        f"immutable prematch prediction conflict: {expected.run_id}/{expected.match_id}"
    )
    if actual is None:
        raise ValueError(conflict)
    expected_immutable = expected.immutable_columns()
    stored_immutable = actual[: len(expected_immutable)]
    if actual[-1] not in {expected.status, "settled"}:
        raise ValueError(conflict)
    if stored_immutable == expected_immutable:
        return expected.dependency_revision
    stored_content = (
        stored_immutable[:_PREDICTION_REVISION_INDEX]
        + stored_immutable[_PREDICTION_REVISION_INDEX + 1 :]
    )
    expected_content = (
        expected_immutable[:_PREDICTION_REVISION_INDEX]
        + expected_immutable[_PREDICTION_REVISION_INDEX + 1 :]
    )
    if stored_content != expected_content:
        raise ValueError(conflict)
    stored_revision = _positive_int(
        int(stored_immutable[_PREDICTION_REVISION_INDEX]),
        "stored dependency_revision",
    )
    if not prematch_dependency_revision_is_current(
        connection,
        dependency_revision=stored_revision,
        prediction_cutoff=expected.prediction_cutoff,
    ):
        raise ValueError(conflict)
    return stored_revision


def _validate_prediction_calibration(
    connection: PostgresSession,
    prediction: PrematchPredictionRecord,
    calibrations_by_hash: Mapping[str, PrematchCalibrationRecord],
) -> None:
    expected_fingerprint = prematch_artifact_fingerprint(
        model_hash=prediction.run_id,
        calibration_hash=prediction.calibration_hash,
    )
    if not hmac.compare_digest(
        prediction.artifact_fingerprint,
        expected_fingerprint,
    ):
        raise ValueError("prediction artifact fingerprint does not recompute")
    if prediction.calibration_hash is None:
        return

    calibration = calibrations_by_hash.get(prediction.calibration_hash)
    if calibration is None:
        stored = connection.execute(
            """SELECT model_hash, status, evaluation_cutoff, artifact_json
                 FROM prematch_calibration_artifacts
                WHERE calibration_hash=?""",
            (prediction.calibration_hash,),
        ).fetchone()
        if stored is None:
            raise ValueError("prematch prediction calibration is unavailable")
        calibration_model_hash = str(stored["model_hash"])
        calibration_status = str(stored["status"])
        evaluation_cutoff = datetime.fromisoformat(
            str(stored["evaluation_cutoff"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        artifact_json = str(stored["artifact_json"])
    else:
        calibration_model_hash = calibration.model_hash
        calibration_status = calibration.status
        evaluation_cutoff = calibration.evaluation_cutoff
        artifact_json = calibration.artifact_json
    artifact = load_prematch_calibration_artifact_json(artifact_json)
    parent = connection.execute(
        """SELECT model_kind, availability_mode
             FROM prematch_model_runs
            WHERE run_id=?""",
        (prediction.run_id,),
    ).fetchone()
    if (
        parent is None
        or calibration_model_hash != prediction.run_id
        or calibration_status in {"unsupported", "failed"}
        or artifact.status.value != calibration_status
        or artifact.model_kind != str(parent["model_kind"])
        or artifact.availability_mode != str(parent["availability_mode"])
        or artifact.calibration_cutoff != evaluation_cutoff
        or evaluation_cutoff >= prediction.prediction_cutoff
        or prediction.raw_probability is None
        or prediction.calibrated_probability is None
    ):
        raise ValueError("prematch prediction calibration identity disagrees")
    application = load_and_apply_prematch_calibration_json(
        artifact_json,
        prediction.raw_probability,
        prediction_cutoff=prediction.prediction_cutoff,
        availability_mode=artifact.availability_mode,
        model_hash=prediction.run_id,
        input_snapshot_hash=prediction.input_snapshot_hash,
    )
    if application.calibrated_probability is None or not math.isclose(
        application.calibrated_probability,
        prediction.calibrated_probability,
        rel_tol=0.0,
        abs_tol=1e-15,
    ):
        raise ValueError("prematch calibrated probability does not replay")


def persist_prematch_records(
    connection: PostgresSession,
    *,
    model_runs: Sequence[PrematchModelRunRecord] = (),
    calibration_artifacts: Sequence[PrematchCalibrationRecord] = (),
    predictions: Sequence[PrematchPredictionRecord] = (),
    validations: Sequence[PrematchValidationRecord] = (),
    dry_run: bool = False,
    created_at: datetime | None = None,
) -> PrematchPersistenceCounts:
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be boolean")
    created = _utc(
        datetime.now(UTC) if created_at is None else created_at,
        "created_at",
    ).isoformat()
    runs = _normalize_records(
        tuple(model_runs),
        key=lambda row: row.run_id,
        stable=lambda row: row.stable_columns(),
        conflict=lambda row: f"immutable prematch model run conflict: {row.run_id}",
    )
    calibrations = _normalize_records(
        tuple(calibration_artifacts),
        key=lambda row: row.calibration_key,
        stable=lambda row: row.stable_columns(),
        conflict=lambda row: (
            "immutable prematch calibration conflict: " + row.calibration_key
        ),
    )
    prediction_rows = _normalize_records(
        tuple(predictions),
        key=lambda row: (row.run_id, row.match_id),
        stable=lambda row: row.stable_columns(),
        conflict=lambda row: (
            f"immutable prematch prediction conflict: {row.run_id}/{row.match_id}"
        ),
    )
    validation_rows = _normalize_records(
        tuple(validations),
        key=lambda row: (row.run_id, row.match_id),
        stable=lambda row: row.stable_columns(),
        conflict=lambda row: (
            f"immutable prematch validation conflict: {row.run_id}/{row.match_id}"
        ),
    )
    if any(row.status == "settled" for row in prediction_rows):
        raise ValueError("settled predictions require the controlled settlement API")
    calibrations_by_hash = {row.calibration_hash: row for row in calibrations}
    predictions_by_identity = {
        (row.run_id, row.match_id): row for row in prediction_rows
    }
    for validation in validation_rows:
        prediction = predictions_by_identity.get(
            (validation.run_id, validation.match_id)
        )
        if prediction is not None and (
            validation.input_snapshot_hash != prediction.input_snapshot_hash
            or validation.artifact_fingerprint != prediction.artifact_fingerprint
            or validation.dependency_fingerprint != prediction.dependency_fingerprint
            or validation.dependency_revision != prediction.dependency_revision
        ):
            raise ValueError("prematch validation claims disagree")

    counts = {
        "inserted_model_runs": 0,
        "unchanged_model_runs": 0,
        "inserted_calibrations": 0,
        "unchanged_calibrations": 0,
        "inserted_predictions": 0,
        "unchanged_predictions": 0,
        "inserted_validations": 0,
        "unchanged_validations": 0,
    }
    effective_prediction_revisions: dict[tuple[str, int], int] = {}
    with connection.transaction():
        for row in runs:
            existing = connection.execute(
                """SELECT model_version, artifact_version, model_kind,
                          availability_mode, training_cutoff,
                          feature_schema_hash, training_input_hash, model_hash,
                          artifact_json, metrics_json, status
                     FROM prematch_model_runs WHERE run_id=?""",
                (row.run_id,),
            ).fetchone()
            if existing is not None:
                _require_exact_row(
                    existing,
                    row.stable_columns(),
                    f"immutable prematch model run conflict: {row.run_id}",
                )
                counts["unchanged_model_runs"] += 1
            elif dry_run:
                counts["inserted_model_runs"] += 1
            else:
                inserted = connection.execute(
                    """INSERT INTO prematch_model_runs
                       (run_id, model_version, artifact_version, model_kind,
                        availability_mode, training_cutoff,
                        feature_schema_hash, training_input_hash, model_hash,
                        artifact_json, metrics_json, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT DO NOTHING RETURNING run_id""",
                    (row.run_id, *row.stable_columns(), created),
                ).fetchone()
                if inserted is not None:
                    counts["inserted_model_runs"] += 1
                else:
                    _require_exact_row(
                        connection.execute(
                            """SELECT model_version, artifact_version, model_kind,
                                      availability_mode, training_cutoff,
                                      feature_schema_hash, training_input_hash,
                                      model_hash, artifact_json, metrics_json, status
                                 FROM prematch_model_runs WHERE run_id=?""",
                            (row.run_id,),
                        ).fetchone(),
                        row.stable_columns(),
                        f"immutable prematch model run conflict: {row.run_id}",
                    )
                    counts["unchanged_model_runs"] += 1

        for row in calibrations:
            parent_record = next(
                (
                    candidate
                    for candidate in runs
                    if candidate.model_hash == row.model_hash
                ),
                None,
            )
            if parent_record is None:
                parent = connection.execute(
                    """SELECT model_kind, availability_mode, training_cutoff
                         FROM prematch_model_runs WHERE model_hash=?""",
                    (row.model_hash,),
                ).fetchone()
                if parent is None:
                    raise ValueError("prematch calibration parent model is unavailable")
                parent_kind = str(parent["model_kind"])
                parent_mode = str(parent["availability_mode"])
                parent_cutoff = datetime.fromisoformat(
                    str(parent["training_cutoff"]).replace("Z", "+00:00")
                ).astimezone(UTC)
            else:
                parent_kind = parent_record.model_kind
                parent_mode = parent_record.availability_mode
                parent_cutoff = parent_record.training_cutoff
            calibration_artifact = load_prematch_calibration_artifact_json(
                row.artifact_json
            )
            if (
                parent_kind != row.model_kind
                or parent_mode != calibration_artifact.availability_mode
                or parent_cutoff > row.evaluation_cutoff
            ):
                raise ValueError("prematch calibration parent identity disagrees")
            existing = connection.execute(
                """SELECT model_kind, model_hash, calibration_version,
                          fit_cutoff, evaluation_cutoff, fit_support,
                          evaluation_support, parameters_json, metrics_json,
                          input_hash, calibration_hash, artifact_json, status
                     FROM prematch_calibration_artifacts
                    WHERE calibration_key=?""",
                (row.calibration_key,),
            ).fetchone()
            if existing is not None:
                _require_exact_row(
                    existing,
                    row.stable_columns(),
                    "immutable prematch calibration conflict: " + row.calibration_key,
                )
                counts["unchanged_calibrations"] += 1
            elif dry_run:
                counts["inserted_calibrations"] += 1
            else:
                inserted = connection.execute(
                    """INSERT INTO prematch_calibration_artifacts
                       (calibration_key, model_kind, model_hash,
                        calibration_version, fit_cutoff, evaluation_cutoff,
                        fit_support, evaluation_support, parameters_json,
                        metrics_json, input_hash, calibration_hash,
                        artifact_json, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT DO NOTHING RETURNING calibration_key""",
                    (row.calibration_key, *row.stable_columns(), created),
                ).fetchone()
                if inserted is not None:
                    counts["inserted_calibrations"] += 1
                else:
                    _require_exact_row(
                        connection.execute(
                            """SELECT model_kind, model_hash,
                                      calibration_version, fit_cutoff,
                                      evaluation_cutoff, fit_support,
                                      evaluation_support, parameters_json,
                                      metrics_json, input_hash, calibration_hash,
                                      artifact_json, status
                                 FROM prematch_calibration_artifacts
                                WHERE calibration_key=?""",
                            (row.calibration_key,),
                        ).fetchone(),
                        row.stable_columns(),
                        "immutable prematch calibration conflict: "
                        + row.calibration_key,
                    )
                    counts["unchanged_calibrations"] += 1

        for row in prediction_rows:
            _validate_prediction_calibration(
                connection,
                row,
                calibrations_by_hash,
            )
            existing = connection.execute(
                """SELECT match_id, prediction_cutoff, cutoff_source,
                          input_snapshot_hash, artifact_fingerprint,
                          dependency_fingerprint, dependency_revision,
                          calibration_hash, team_base_probability,
                          raw_probability, calibrated_probability,
                          parameter_uncertainty, draft_logit_delta,
                          rosh_logit_delta, cluster_logit_delta,
                          total_adjustment, coverage, support, prediction_json,
                          eventual_radiant_win, result_usable_at, settled_at,
                          status
                     FROM prematch_predictions
                    WHERE run_id=? AND match_id=?""",
                (row.run_id, row.match_id),
            ).fetchone()
            if existing is not None:
                effective_prediction_revisions[(row.run_id, row.match_id)] = (
                    _stored_prediction_revision(connection, existing, row)
                )
                counts["unchanged_predictions"] += 1
            elif dry_run:
                effective_prediction_revisions[(row.run_id, row.match_id)] = (
                    row.dependency_revision
                )
                counts["inserted_predictions"] += 1
            else:
                inserted = connection.execute(
                    """INSERT INTO prematch_predictions
                       (run_id, match_id, prediction_cutoff, cutoff_source,
                        input_snapshot_hash, artifact_fingerprint,
                        dependency_fingerprint, dependency_revision,
                        calibration_hash, team_base_probability,
                        raw_probability, calibrated_probability,
                        parameter_uncertainty, draft_logit_delta,
                        rosh_logit_delta, cluster_logit_delta,
                        total_adjustment, coverage, support, prediction_json,
                        eventual_radiant_win, result_usable_at, settled_at,
                        status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT DO NOTHING RETURNING prediction_id""",
                    (row.run_id, *row.stable_columns(), created),
                ).fetchone()
                if inserted is not None:
                    effective_prediction_revisions[(row.run_id, row.match_id)] = (
                        row.dependency_revision
                    )
                    counts["inserted_predictions"] += 1
                else:
                    stored = connection.execute(
                        """SELECT match_id, prediction_cutoff, cutoff_source,
                                  input_snapshot_hash, artifact_fingerprint,
                                  dependency_fingerprint, dependency_revision,
                                  calibration_hash, team_base_probability,
                                  raw_probability, calibrated_probability,
                                  parameter_uncertainty, draft_logit_delta,
                                  rosh_logit_delta, cluster_logit_delta,
                                  total_adjustment, coverage, support,
                                  prediction_json, eventual_radiant_win,
                                  result_usable_at, settled_at, status
                             FROM prematch_predictions
                            WHERE run_id=? AND match_id=?""",
                        (row.run_id, row.match_id),
                    ).fetchone()
                    effective_prediction_revisions[(row.run_id, row.match_id)] = (
                        _stored_prediction_revision(connection, stored, row)
                    )
                    counts["unchanged_predictions"] += 1

        for original_row in validation_rows:
            effective_revision = effective_prediction_revisions.get(
                (original_row.run_id, original_row.match_id),
                original_row.dependency_revision,
            )
            row = (
                original_row
                if effective_revision == original_row.dependency_revision
                else replace(original_row, dependency_revision=effective_revision)
            )
            existing = connection.execute(
                """SELECT input_snapshot_hash, artifact_fingerprint,
                          dependency_fingerprint, dependency_revision,
                          validation_version, validated_at
                     FROM prematch_prediction_validations
                    WHERE run_id=? AND match_id=?""",
                (row.run_id, row.match_id),
            ).fetchone()
            if existing is not None:
                _require_exact_row(
                    existing,
                    row.stable_columns(),
                    "immutable prematch validation conflict: "
                    f"{row.run_id}/{row.match_id}",
                )
                counts["unchanged_validations"] += 1
            elif dry_run:
                counts["inserted_validations"] += 1
            else:
                inserted = connection.execute(
                    """INSERT INTO prematch_prediction_validations
                       (run_id, match_id, input_snapshot_hash,
                        artifact_fingerprint, dependency_fingerprint,
                        dependency_revision, validation_version, validated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT DO NOTHING RETURNING run_id""",
                    (row.run_id, row.match_id, *row.stable_columns()),
                ).fetchone()
                if inserted is not None:
                    counts["inserted_validations"] += 1
                else:
                    _require_exact_row(
                        connection.execute(
                            """SELECT input_snapshot_hash,
                                      artifact_fingerprint,
                                      dependency_fingerprint,
                                      dependency_revision, validation_version,
                                      validated_at
                                 FROM prematch_prediction_validations
                                WHERE run_id=? AND match_id=?""",
                            (row.run_id, row.match_id),
                        ).fetchone(),
                        row.stable_columns(),
                        "immutable prematch validation conflict: "
                        f"{row.run_id}/{row.match_id}",
                    )
                    counts["unchanged_validations"] += 1
    return PrematchPersistenceCounts(**counts)


def load_prematch_model_artifact(
    connection: PostgresSession,
    run_id: str,
) -> PrematchModelArtifact:
    identity = _sha256(run_id, "run_id")
    row = connection.execute(
        """SELECT model_version, artifact_version, model_kind,
                  availability_mode, training_cutoff, feature_schema_hash,
                  training_input_hash, model_hash, artifact_json, status
             FROM prematch_model_runs WHERE run_id=?""",
        (identity,),
    ).fetchone()
    if row is None:
        raise ValueError("prematch model run is unavailable")
    artifact = load_prematch_model_artifact_json(str(row["artifact_json"]))
    expected = (
        artifact.model_version,
        artifact.artifact_version,
        artifact.model_kind,
        artifact.availability_mode,
        artifact.training_cutoff.isoformat(),
        artifact.feature_schema_hash,
        artifact.training_input_hash,
        artifact.model_hash,
        artifact.status.value,
    )
    actual = (
        str(row["model_version"]),
        str(row["artifact_version"]),
        str(row["model_kind"]),
        str(row["availability_mode"]),
        str(row["training_cutoff"]),
        str(row["feature_schema_hash"]),
        str(row["training_input_hash"]),
        str(row["model_hash"]),
        str(row["status"]),
    )
    if identity != artifact.model_hash or actual != expected:
        raise ValueError("stored prematch model artifact identity does not replay")
    return artifact


def settle_prematch_prediction(
    connection: PostgresSession,
    *,
    run_id: str,
    match_id: int,
    eventual_radiant_win: bool,
    result_usable_at: datetime,
    settled_at: datetime,
) -> PrematchSettlementResult:
    identity = _sha256(run_id, "run_id")
    target = _positive_int(match_id, "match_id")
    if not isinstance(eventual_radiant_win, bool):
        raise ValueError("eventual_radiant_win must be boolean")
    usable = _utc(result_usable_at, "result_usable_at")
    settled = _utc(settled_at, "settled_at")
    if usable > settled:
        raise ValueError("result_usable_at follows settled_at")
    with connection.transaction():
        row = connection.execute(
            """SELECT status, eventual_radiant_win,
                      result_usable_at, settled_at
                 FROM prematch_predictions
                WHERE run_id=? AND match_id=? FOR UPDATE""",
            (identity, target),
        ).fetchone()
        if row is None:
            raise ValueError("prematch prediction is unavailable")
        if str(row["status"]) == "settled":
            expected = (
                int(eventual_radiant_win),
                usable.isoformat(),
                settled.isoformat(),
            )
            actual = (
                int(row["eventual_radiant_win"]),
                str(row["result_usable_at"]),
                str(row["settled_at"]),
            )
            if actual != expected:
                raise ValueError("immutable prematch settlement conflict")
            return PrematchSettlementResult(updated=False, unchanged=True)
        if str(row["status"]) != "predicted":
            raise ValueError("only predicted prematch rows can settle")
        changed = connection.execute(
            """UPDATE prematch_predictions
                  SET eventual_radiant_win=?, result_usable_at=?,
                      settled_at=?, status='settled'
                WHERE run_id=? AND match_id=? AND status='predicted'""",
            (
                int(eventual_radiant_win),
                usable.isoformat(),
                settled.isoformat(),
                identity,
                target,
            ),
        )
        if changed.rowcount != 1:
            raise ValueError("prematch settlement transition raced")
    return PrematchSettlementResult(updated=True, unchanged=False)


def prematch_prediction_is_stale(
    connection: PostgresSession,
    *,
    run_id: str,
    match_id: int,
) -> bool:
    identity = _sha256(run_id, "run_id")
    target = _positive_int(match_id, "match_id")
    row = connection.execute(
        """SELECT prematch_lineage_revision_is_current(
                      validation.dependency_revision,
                      prediction.prediction_cutoff
                  ) AS is_current
             FROM prematch_predictions AS prediction
             JOIN prematch_prediction_validations AS validation
               ON validation.run_id=prediction.run_id
              AND validation.match_id=prediction.match_id
            WHERE prediction.run_id=? AND prediction.match_id=?
              AND validation.input_snapshot_hash=prediction.input_snapshot_hash
              AND validation.artifact_fingerprint=prediction.artifact_fingerprint
              AND validation.dependency_fingerprint=prediction.dependency_fingerprint""",
        (identity, target),
    ).fetchone()
    return row is None or not bool(row["is_current"])


def current_prematch_lineage_revisions(
    connection: PostgresSession,
) -> tuple[int, int]:
    row = connection.execute(
        """SELECT dependency_revision, artifact_revision
             FROM prematch_lineage_revisions WHERE singleton=1"""
    ).fetchone()
    if row is None:
        raise ValueError("prematch lineage revision authority is unavailable")
    return (
        _positive_int(int(row["dependency_revision"]), "dependency_revision"),
        _positive_int(int(row["artifact_revision"]), "artifact_revision"),
    )


__all__ = [
    "PREMATCH_VALIDATION_VERSION",
    "PrematchCalibrationRecord",
    "PrematchModelRunRecord",
    "PrematchPersistenceCounts",
    "PrematchPredictionRecord",
    "PrematchSettlementResult",
    "PrematchValidationRecord",
    "build_prematch_calibration_record",
    "build_prematch_model_run_record",
    "build_prematch_prediction_record",
    "build_prematch_validation_record",
    "current_prematch_lineage_revisions",
    "load_prematch_model_artifact",
    "persist_prematch_records",
    "prematch_artifact_fingerprint",
    "prematch_dependency_revision_is_current",
    "prematch_dependency_fingerprint",
    "prematch_prediction_is_stale",
    "require_prematch_dependency_revision_current",
    "settle_prematch_prediction",
]
