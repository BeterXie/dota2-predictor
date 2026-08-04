"""Strict loading and full-refit replay for M5 prematch model artifacts."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from datetime import datetime, timezone
from numbers import Real
from typing import Any, Mapping

from .draft_features import AvailabilityMode
from .prematch_features import (
    PREMATCH_FEATURE_VERSION,
    prematch_feature_schema,
    prematch_feature_schema_hash,
)
from .prematch_model import (
    PREMATCH_FTOL,
    PREMATCH_GTOL,
    PREMATCH_MAX_ITERATIONS,
    PREMATCH_MAX_LINE_SEARCH_STEPS,
    PREMATCH_MODEL_ARTIFACT_VERSION,
    PREMATCH_MODEL_VERSION,
    PREMATCH_PINV_RCOND,
    PREMATCH_SOLVER_METHOD,
    PREMATCH_TRAINER_RUNTIME,
    ModelStatus,
    PrematchModelArtifact,
    PrematchTrainingCorpusRow,
    fit_prematch_model,
    verify_prematch_model_artifact,
)
from .raw_archive import canonical_json_bytes as _canonical_json_bytes


UTC = timezone.utc

_MODEL_FIELDS = frozenset(
    {
        "artifact_version",
        "model_version",
        "feature_version",
        "model_kind",
        "status",
        "reason",
        "availability_mode",
        "training_cutoff",
        "support",
        "series_support",
        "event_support",
        "patch_support",
        "min_samples",
        "l2_regularization",
        "solver",
        "feature_names",
        "feature_schema_hash",
        "training_input_hash",
        "trainer_runtime",
        "training_corpus",
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
_SOLVER_FIELDS = frozenset(
    {
        "method",
        "max_iterations",
        "ftol",
        "gtol",
        "max_line_search_steps",
        "pinv_rcond",
    }
)
_CORPUS_FIELDS = frozenset(
    {
        "match_id",
        "input_snapshot_hash",
        "prediction_cutoff",
        "completed_at",
        "result_usable_at",
        "availability_mode",
        "outcome",
        "series_id",
        "event_id",
        "patch_id",
        "team_base_logit",
        "features",
        "missing_features",
    }
)


def canonical_json_bytes(payload: object) -> bytes:
    return _canonical_json_bytes(payload)


def canonical_hash(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


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
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown: {', '.join(unknown)}")
        raise ValueError(f"{label} keys do not match ({'; '.join(details)})")
    return value


def _strict_json_object(payload_json: str) -> Mapping[str, Any]:
    if not isinstance(payload_json, str):
        raise ValueError("prematch model artifact JSON must be a string")

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
        raise ValueError("prematch model artifact JSON is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("prematch model artifact must be an object")
    return payload


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


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _mode(value: object) -> str:
    try:
        return AvailabilityMode(value).value
    except (TypeError, ValueError) as error:
        raise ValueError("unsupported prematch availability mode") from error


def _array(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _named_float_pairs(
    value: object,
    field: str,
    *,
    expected_names: tuple[str, ...] | None = None,
) -> tuple[tuple[str, float], ...]:
    if not isinstance(value, Mapping) or any(
        not isinstance(name, str) for name in value
    ):
        raise ValueError(f"{field} must be an object")
    if expected_names is not None and frozenset(value) != frozenset(expected_names):
        raise ValueError(f"{field} keys do not match feature_names")
    names = expected_names if expected_names is not None else tuple(sorted(value))
    return tuple((name, _finite(value[name], f"{field}.{name}")) for name in names)


def _corpus_row_from_payload(
    value: object,
    *,
    feature_names: tuple[str, ...],
) -> PrematchTrainingCorpusRow:
    payload = _exact_object(value, _CORPUS_FIELDS, "training corpus row")
    features_raw = _exact_object(
        payload["features"],
        frozenset(feature_names),
        "training corpus features",
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
    missing_raw = _array(payload["missing_features"], "missing_features")
    if any(not isinstance(name, str) for name in missing_raw):
        raise ValueError("missing_features must contain strings")
    expected_missing = tuple(name for name, value in features if value is None)
    if tuple(missing_raw) != expected_missing:
        raise ValueError("missing_features do not match training features")
    outcome = _integer(payload["outcome"], "outcome")
    if outcome not in (0, 1):
        raise ValueError("outcome must be 0 or 1")
    row = PrematchTrainingCorpusRow(
        match_id=_integer(payload["match_id"], "match_id", minimum=1),
        input_snapshot_hash=_digest(
            payload["input_snapshot_hash"],
            "input_snapshot_hash",
        ),
        prediction_cutoff=_parse_utc(
            payload["prediction_cutoff"],
            "prediction_cutoff",
        ),
        completed_at=_parse_utc(payload["completed_at"], "completed_at"),
        result_usable_at=_parse_utc(
            payload["result_usable_at"],
            "result_usable_at",
        ),
        availability_mode=_mode(payload["availability_mode"]),
        outcome=outcome,
        series_id=_nonempty(payload["series_id"], "series_id"),
        event_id=_nonempty(payload["event_id"], "event_id"),
        patch_id=_nonempty(payload["patch_id"], "patch_id"),
        team_base_logit=_finite(payload["team_base_logit"], "team_base_logit"),
        features=features,
        missing_features=expected_missing,
    )
    if canonical_json_bytes(row.to_payload()) != canonical_json_bytes(dict(payload)):
        raise ValueError("training corpus row is not canonical")
    return row


def prematch_model_artifact_from_payload(
    payload: Mapping[str, Any],
) -> PrematchModelArtifact:
    row = _exact_object(payload, _MODEL_FIELDS, "prematch model artifact")
    if row["artifact_version"] != PREMATCH_MODEL_ARTIFACT_VERSION:
        raise ValueError("unsupported prematch model artifact version")
    if row["model_version"] != PREMATCH_MODEL_VERSION:
        raise ValueError("unsupported prematch model version")
    if row["feature_version"] != PREMATCH_FEATURE_VERSION:
        raise ValueError("unsupported prematch feature version")
    model_kind = _nonempty(row["model_kind"], "model_kind")
    expected_names = prematch_feature_schema(model_kind)
    feature_names_raw = _array(row["feature_names"], "feature_names")
    if tuple(feature_names_raw) != expected_names:
        raise ValueError("feature_names do not match fixed model schema")
    if row["feature_schema_hash"] != prematch_feature_schema_hash(model_kind):
        raise ValueError("feature_schema_hash does not match fixed model schema")

    solver = _exact_object(row["solver"], _SOLVER_FIELDS, "solver")
    if (
        solver["method"] != PREMATCH_SOLVER_METHOD
        or _integer(solver["max_iterations"], "max_iterations", minimum=1)
        != PREMATCH_MAX_ITERATIONS
        or _finite(solver["ftol"], "ftol") != PREMATCH_FTOL
        or _finite(solver["gtol"], "gtol") != PREMATCH_GTOL
        or _integer(
            solver["max_line_search_steps"],
            "max_line_search_steps",
            minimum=1,
        )
        != PREMATCH_MAX_LINE_SEARCH_STEPS
        or _finite(solver["pinv_rcond"], "pinv_rcond") != PREMATCH_PINV_RCOND
    ):
        raise ValueError("prematch solver contract does not match")

    runtime_raw = _exact_object(
        row["trainer_runtime"],
        frozenset(name for name, _version in PREMATCH_TRAINER_RUNTIME),
        "trainer_runtime",
    )
    runtime = tuple(
        (name, _nonempty(runtime_raw[name], f"trainer_runtime.{name}"))
        for name, _version in PREMATCH_TRAINER_RUNTIME
    )
    corpus = tuple(
        _corpus_row_from_payload(value, feature_names=expected_names)
        for value in _array(row["training_corpus"], "training_corpus")
    )
    class_counts_raw = _exact_object(
        row["class_counts"],
        frozenset({"0", "1"}),
        "class_counts",
    )
    class_counts = tuple(
        (label, _integer(class_counts_raw[str(label)], f"class_counts.{label}"))
        for label in (0, 1)
    )
    missing_raw = _exact_object(
        row["missing_counts"],
        frozenset(expected_names),
        "missing_counts",
    )
    missing_counts = tuple(
        (name, _integer(missing_raw[name], f"missing_counts.{name}"))
        for name in expected_names
    )
    covariance_raw = _array(row["logit_covariance"], "logit_covariance")
    covariance: list[tuple[float, ...]] = []
    for covariance_row in covariance_raw:
        covariance.append(
            tuple(
                _finite(number, "logit_covariance")
                for number in _array(covariance_row, "logit_covariance row")
            )
        )
    status = ModelStatus(row["status"])
    reason = row["reason"]
    if reason is not None:
        reason = _nonempty(reason, "reason")
    intercept = row["intercept"]
    artifact = PrematchModelArtifact(
        artifact_version=row["artifact_version"],
        model_version=row["model_version"],
        feature_version=row["feature_version"],
        model_kind=model_kind,
        status=status,
        reason=reason,
        availability_mode=_mode(row["availability_mode"]),
        training_cutoff=_parse_utc(row["training_cutoff"], "training_cutoff"),
        support=_integer(row["support"], "support"),
        series_support=_integer(row["series_support"], "series_support"),
        event_support=_integer(row["event_support"], "event_support"),
        patch_support=_integer(row["patch_support"], "patch_support"),
        min_samples=_integer(row["min_samples"], "min_samples", minimum=2),
        l2_regularization=_finite(
            row["l2_regularization"],
            "l2_regularization",
        ),
        feature_names=expected_names,
        feature_schema_hash=_digest(
            row["feature_schema_hash"],
            "feature_schema_hash",
        ),
        training_input_hash=_digest(
            row["training_input_hash"],
            "training_input_hash",
        ),
        trainer_runtime=runtime,
        training_corpus=corpus,
        class_counts=class_counts,
        missing_counts=missing_counts,
        imputation_values=_named_float_pairs(
            row["imputation_values"],
            "imputation_values",
            expected_names=expected_names,
        ),
        standardization_means=_named_float_pairs(
            row["standardization_means"],
            "standardization_means",
            expected_names=expected_names,
        ),
        standardization_scales=_named_float_pairs(
            row["standardization_scales"],
            "standardization_scales",
            expected_names=expected_names,
        ),
        coefficients=_named_float_pairs(
            row["coefficients"],
            "coefficients",
            expected_names=(expected_names if status is ModelStatus.TRAINED else ()),
        ),
        intercept=(None if intercept is None else _finite(intercept, "intercept")),
        logit_covariance=tuple(covariance),
        model_hash=_digest(row["model_hash"], "model_hash"),
    )
    verify_prematch_model_artifact(artifact)
    if canonical_json_bytes(artifact.to_payload()) != canonical_json_bytes(dict(row)):
        raise ValueError("prematch model artifact payload is not canonical")
    return artifact


def model_artifact_from_payload(payload: Mapping[str, Any]) -> PrematchModelArtifact:
    return prematch_model_artifact_from_payload(payload)


def load_prematch_model_artifact_json(payload_json: str) -> PrematchModelArtifact:
    return prematch_model_artifact_from_payload(_strict_json_object(payload_json))


def load_model_artifact_json(payload_json: str) -> PrematchModelArtifact:
    return load_prematch_model_artifact_json(payload_json)


def replay_prematch_model_artifact(
    artifact: PrematchModelArtifact,
) -> PrematchModelArtifact:
    verify_prematch_model_artifact(artifact)
    replayed = fit_prematch_model(
        (row.to_training_row() for row in artifact.training_corpus),
        artifact.training_cutoff,
        model_kind=artifact.model_kind,
        availability_mode=artifact.availability_mode,
        min_samples=artifact.min_samples,
        l2_regularization=artifact.l2_regularization,
    )
    actual = canonical_json_bytes(artifact.to_payload(include_model_hash=False))
    expected = canonical_json_bytes(replayed.to_payload(include_model_hash=False))
    if not hmac.compare_digest(actual, expected):
        raise ValueError("prematch model does not replay from its training corpus")
    return replayed


def assert_prematch_model_artifact_deployable(
    artifact: PrematchModelArtifact,
) -> None:
    verify_prematch_model_artifact(artifact)


__all__ = [
    "assert_prematch_model_artifact_deployable",
    "canonical_hash",
    "canonical_json_bytes",
    "load_model_artifact_json",
    "load_prematch_model_artifact_json",
    "model_artifact_from_payload",
    "prematch_model_artifact_from_payload",
    "replay_prematch_model_artifact",
]
