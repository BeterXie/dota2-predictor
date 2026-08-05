"""Strict, replayable frozen deployment contract for the prematch model."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from numbers import Real
from types import MappingProxyType
from typing import Any, Callable, Mapping

from prematch.stratz_official_profile import get_profile

from .draft_features import AvailabilityMode
from .prematch_artifacts import (
    canonical_json_bytes,
    prematch_model_artifact_from_payload,
)
from .prematch_backtest import PREMATCH_BACKTEST_VERSION
from .prematch_calibration import (
    CalibrationStatus,
    PrematchCalibrationArtifact,
    prematch_calibration_artifact_from_payload,
    replay_prematch_calibration_artifact,
)
from .prematch_features import (
    DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
    DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
    DRAFT_RESIDUAL_MODEL_SCHEMA,
    PREMATCH_FEATURE_VERSION,
    PREMATCH_MODEL_KINDS,
    PrematchFeatureSnapshot,
    prematch_feature_schema_hash,
    verify_prematch_feature_snapshot,
)
from .prematch_model import (
    ModelStatus,
    PrematchModelArtifact,
    verify_prematch_model_artifact,
)
from .prematch_storage import prematch_dependency_fingerprint
from .prematch_report import (
    PREMATCH_MODEL_RUN_METRICS_SCHEMA,
    PrematchBacktestReport,
    report_as_dict,
)
from .rosh_features import ROSH_MODEL_SCHEMA, ROSH_MODEL_SCHEMA_HASH
from .team_rating_artifacts import (
    TeamRatingArtifact,
    team_rating_artifact_from_payload,
    verify_team_rating_artifact,
)


UTC = timezone.utc
PREMATCH_DEPLOYMENT_VERSION = "prematch-frozen-deployment-v1"
PREMATCH_DEPLOYMENT_SCHEMA = "prematch-frozen-deployment/v1"
PREMATCH_DEPLOYMENT_PROOF_SCHEMA = "prematch-m6-default-decision-proof/v1"

_DEPLOYMENT_FIELDS = frozenset(
    {
        "schema",
        "deployment_version",
        "deployment_key",
        "training_cutoff",
        "availability_mode",
        "dependency_fingerprint",
        "dependency_revision",
        "team_rating_artifact",
        "feature_snapshot",
        "prematch_model_artifact",
        "calibration_artifact",
        "m6_report",
        "m6_report_hash",
    }
)
_FEATURE_FIELDS = frozenset(
    {
        "feature_version",
        "match_id",
        "prediction_cutoff",
        "availability_mode",
        "support",
        "coverage",
        "missing_reason",
        "team_base_logit",
        "team_rating",
        "draft_residual",
        "rosh",
        "input_hash",
    }
)
_TEAM_FEATURE_FIELDS = frozenset(
    {
        "run_id",
        "artifact_hash",
        "prediction_input_hash",
        "combined_training_input_hash",
        "support",
    }
)
_DRAFT_FEATURE_FIELDS = frozenset(
    {
        "feature_version",
        "feature_schema_hash",
        "model_schema_hash",
        "input_hash",
        "authority_fingerprint",
        "team_rating_input_hash",
        "support",
        "coverage",
        "features",
    }
)
_ROSH_FEATURE_FIELDS = frozenset(
    {
        "feature_version",
        "model_schema_hash",
        "status",
        "missing_reason",
        "input_hash",
        "run_id",
        "evidence_hash",
        "formula_version",
        "profile_hash",
        "result_hash",
        "coverage",
        "features",
    }
)
_DEFAULT_DECISION_FIELDS = frozenset({"model_kind", "status", "reasons"})
_CALIBRATION_PROOF_FIELDS = frozenset(
    {"model_kind", "status", "gate_passed", "gate_reasons", "calibration_hash"}
)
_INCREMENTAL_PROOF_FIELDS = frozenset(
    {
        "comparison",
        "added_component",
        "available_support",
        "status",
        "reasons",
        "metrics",
    }
)
_PAIRED_METRIC_FIELDS = frozenset(
    {"metric", "delta", "ci_90", "ci_95", "probability_of_improvement"}
)
_BOOTSTRAP_INTERVAL_FIELDS = frozenset({"lower", "upper"})
_M6_REPORT_FIELDS = frozenset(
    {
        # Full PrematchBacktestReport payload.
        "backtest_version",
        "bootstrap_algorithm",
        "bootstrap_seed_material",
        "bootstrap_samples",
        "availability_mode",
        "formal_maps",
        "eligible_targets",
        "snapshot_targets",
        "unavailable_targets",
        "model_slices",
        "incremental_comparisons",
        "calibration",
        "default_decision",
        # Compact prematch_model_run_metrics / deployment proof payloads.
        "schema",
        "bootstrap",
        "formal_evaluation",
        "slice",
        "calibration_artifacts",
        "model_hash",
    }
)


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(_thaw(value))).hexdigest()


def canonical_hash(value: object) -> str:
    """Return the deployment module's canonical SHA-256 identity."""

    return _hash(value)


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


def _optional_finite(value: object, field: str) -> float | None:
    return None if value is None else _finite(value, field)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


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


def _mode(value: object) -> str:
    try:
        return AvailabilityMode(value).value
    except (TypeError, ValueError) as error:
        raise ValueError("unsupported prematch availability mode") from error


def _exact_object(
    value: object, fields: frozenset[str], field: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} must be an object")
    actual = frozenset(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown: {', '.join(unknown)}")
        raise ValueError(f"{field} keys do not match ({'; '.join(details)})")
    return value


def _array(value: object, field: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be an array")
    return list(value)


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, list):
        return [_thaw(item) for item in value]
    return value


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _require_json_object_keys(value: object, field: str) -> None:
    """Reject mappings that would be silently coerced by JSON encoding."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field} object keys must be strings")
            _require_json_object_keys(item, field)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _require_json_object_keys(item, field)


def _strict_json_object(payload_json: str) -> Mapping[str, Any]:
    if not isinstance(payload_json, str):
        raise ValueError("prematch deployment JSON must be a string")

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
        raise ValueError("prematch deployment JSON is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("prematch deployment must be an object")
    canonical = canonical_json_bytes(payload).decode("utf-8")
    if not hmac.compare_digest(payload_json.encode("utf-8"), canonical.encode("utf-8")):
        raise ValueError("prematch deployment JSON is not canonical")
    return payload


def _report_payload(
    report: PrematchBacktestReport | Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if report is not None and payload is not None:
        first = _report_payload(report, None)
        second = _report_payload(None, payload)
        if canonical_json_bytes(first) != canonical_json_bytes(second):
            raise ValueError("M6 report and report payload disagree")
        return first
    if report is not None:
        if isinstance(report, PrematchBacktestReport):
            value = report_as_dict(report)
        elif isinstance(report, Mapping):
            _require_json_object_keys(report, "M6 report payload")
            thawed = _thaw(report)
            if not isinstance(thawed, Mapping):
                raise ValueError("M6 report must be an object")
            value = dict(thawed)
        else:
            raise ValueError("M6 report must be a PrematchBacktestReport or object")
    elif payload is not None:
        if not isinstance(payload, Mapping):
            raise ValueError("M6 report payload must be an object")
        _require_json_object_keys(payload, "M6 report payload")
        thawed = _thaw(payload)
        if not isinstance(thawed, Mapping):
            raise ValueError("M6 report payload must be an object")
        value = dict(thawed)
    else:
        raise ValueError("M6 report proof is required")
    # Force JSON compatibility and detach all mutable caller-owned containers.
    try:
        _require_json_object_keys(value, "M6 report payload")
        canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise ValueError("M6 report payload is not canonical JSON data") from error
    return value


def _feature_snapshot_from_payload(
    value: object,
    *,
    team_base_logit: float | None = None,
) -> PrematchFeatureSnapshot:
    row = _exact_object(value, _FEATURE_FIELDS, "prematch feature snapshot")
    team = _exact_object(
        row["team_rating"], _TEAM_FEATURE_FIELDS, "team rating snapshot"
    )
    draft = _exact_object(
        row["draft_residual"], _DRAFT_FEATURE_FIELDS, "draft residual snapshot"
    )
    rosh = _exact_object(row["rosh"], _ROSH_FEATURE_FIELDS, "R.O.S.H. snapshot")

    draft_features_raw = draft["features"]
    if not isinstance(draft_features_raw, Mapping):
        raise ValueError("draft residual features must be an object")
    if frozenset(draft_features_raw) != frozenset(DRAFT_RESIDUAL_MODEL_SCHEMA):
        raise ValueError("draft residual feature keys do not match schema")
    rosh_features_raw = rosh["features"]
    if not isinstance(rosh_features_raw, Mapping):
        raise ValueError("R.O.S.H. features must be an object")
    if frozenset(rosh_features_raw) != frozenset(ROSH_MODEL_SCHEMA):
        raise ValueError("R.O.S.H. feature keys do not match schema")

    claimed_team_logit = _finite(row["team_base_logit"], "team_base_logit")
    if team_base_logit is not None and not math.isclose(
        claimed_team_logit,
        _finite(team_base_logit, "team_base_logit"),
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError("feature snapshot Team Rating logit does not replay")
    snapshot = PrematchFeatureSnapshot(
        feature_version=row["feature_version"],
        match_id=_positive_int(row["match_id"], "match_id"),
        prediction_cutoff=_parse_utc(row["prediction_cutoff"], "prediction_cutoff"),
        availability_mode=_mode(row["availability_mode"]),
        team_base_logit=claimed_team_logit,
        team_rating_run_id=_digest(team["run_id"], "team_rating.run_id"),
        team_rating_artifact_hash=_digest(
            team["artifact_hash"], "team_rating.artifact_hash"
        ),
        team_rating_prediction_input_hash=_digest(
            team["prediction_input_hash"], "team_rating.prediction_input_hash"
        ),
        team_rating_combined_training_input_hash=_digest(
            team["combined_training_input_hash"],
            "team_rating.combined_training_input_hash",
        ),
        team_rating_support=_nonnegative_int(team["support"], "team_rating.support"),
        draft_residual_input_hash=_digest(
            draft["input_hash"], "draft_residual.input_hash"
        ),
        draft_residual_authority_fingerprint=_digest(
            draft["authority_fingerprint"], "draft_residual.authority_fingerprint"
        ),
        draft_residual_team_rating_input_hash=_digest(
            draft["team_rating_input_hash"], "draft_residual.team_rating_input_hash"
        ),
        draft_residual_feature_schema_hash=_digest(
            draft["feature_schema_hash"], "draft_residual.feature_schema_hash"
        ),
        draft_residual_model_schema_hash=_digest(
            draft["model_schema_hash"], "draft_residual.model_schema_hash"
        ),
        draft_support=_nonnegative_int(draft["support"], "draft_residual.support"),
        draft_coverage=_probability(draft["coverage"], "draft_residual.coverage"),
        draft_features=tuple(
            (name, draft_features_raw[name]) for name in DRAFT_RESIDUAL_MODEL_SCHEMA
        ),
        rosh_status=rosh["status"],
        rosh_missing_reason=rosh["missing_reason"],
        rosh_input_hash=_digest(rosh["input_hash"], "rosh.input_hash"),
        rosh_model_schema_hash=_digest(
            rosh["model_schema_hash"], "rosh.model_schema_hash"
        ),
        rosh_run_id=None
        if rosh["run_id"] is None
        else _digest(rosh["run_id"], "rosh.run_id"),
        rosh_evidence_hash=None
        if rosh["evidence_hash"] is None
        else _digest(rosh["evidence_hash"], "rosh.evidence_hash"),
        rosh_formula_version=rosh["formula_version"],
        rosh_profile_hash=None
        if rosh["profile_hash"] is None
        else _digest(rosh["profile_hash"], "rosh.profile_hash"),
        rosh_result_hash=None
        if rosh["result_hash"] is None
        else _digest(rosh["result_hash"], "rosh.result_hash"),
        rosh_coverage=_probability(rosh["coverage"], "rosh.coverage"),
        rosh_features=tuple(
            (name, rosh_features_raw[name]) for name in ROSH_MODEL_SCHEMA
        ),
        input_hash=_digest(row["input_hash"], "input_hash"),
    )
    if canonical_json_bytes(snapshot.to_payload()) != canonical_json_bytes(dict(row)):
        raise ValueError("prematch feature snapshot payload is not canonical")
    return snapshot


def _default_decision(report: Mapping[str, Any]) -> Mapping[str, Any]:
    value = report.get("default_decision")
    if not isinstance(value, Mapping):
        raise ValueError("M6 report lacks default decision proof")
    return _exact_object(value, _DEFAULT_DECISION_FIELDS, "M6 default decision")


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    rows = tuple(_nonempty(item, field) for item in _array(value, field))
    if rows != tuple(sorted(set(rows))):
        raise ValueError(f"{field} must be sorted and unique")
    return rows


def _report_default_decision(
    report: Mapping[str, Any],
) -> tuple[str, str, tuple[str, ...]]:
    decision = _default_decision(report)
    model_kind = decision["model_kind"]
    if model_kind is None:
        raise ValueError("M6 report has no deployable default model")
    model_kind = _nonempty(model_kind, "default_decision.model_kind")
    if model_kind not in PREMATCH_MODEL_KINDS:
        raise ValueError("unsupported default prematch model kind")
    if model_kind == "team_only":
        raise ValueError("team_only is not a deployable M6 default model")
    status = _nonempty(decision["status"], "default_decision.status")
    reasons = tuple(
        _nonempty(item, "default_decision.reasons")
        for item in _array(decision["reasons"], "default_decision.reasons")
    )
    return model_kind, status, reasons


def _report_calibration_proof(
    report: Mapping[str, Any],
    *,
    model_kind: str,
    calibration: PrematchCalibrationArtifact,
) -> None:
    rows = report.get("calibration")
    if rows is None:
        # A compact proof may carry one calibration object instead of the full
        # M6 report.  It is still required to bind every identity used below.
        rows = report.get("calibration_artifacts")
    if not isinstance(rows, (list, tuple)):
        raise ValueError("M6 report lacks calibration gate proof")
    matching: list[Mapping[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("M6 calibration proof rows must be objects")
        if raw.get("model_kind") == model_kind:
            matching.append(raw)
    if len(matching) != 1:
        raise ValueError("M6 report has no unique calibration proof for model")
    row = matching[0]
    missing_fields = _CALIBRATION_PROOF_FIELDS - frozenset(row)
    if missing_fields:
        raise ValueError(
            "M6 calibration proof lacks "
            + ", ".join(sorted(missing_fields))
        )
    if row["gate_passed"] is not True:
        raise ValueError("M6 calibration gate did not pass")
    if not hmac.compare_digest(
        _digest(row["calibration_hash"], "calibration proof hash"),
        calibration.calibration_hash,
    ):
        raise ValueError("M6 calibration proof hash disagrees")
    if "status" in row and row["status"] != calibration.status.value:
        raise ValueError("M6 calibration proof status disagrees")
    reasons = row["gate_reasons"]
    if not isinstance(reasons, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in reasons
    ):
        raise ValueError("M6 calibration gate reasons are invalid")
    if tuple(reasons) != calibration.gate_reasons:
        raise ValueError("M6 calibration gate reasons disagree")
    if reasons:
        raise ValueError("a passed M6 calibration gate cannot have reasons")


_REQUIRED_INCREMENTAL_COMPARISONS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "team_plus_draft": ("M3-M2",),
        "team_plus_rosh": ("M4-M2",),
        # M5-M2 is emitted as an additional diagnostic by the current M6
        # report, but the design contract requires only the two component
        # comparisons for the combined candidate.
        "team_plus_draft_rosh": ("M5-M3", "M5-M4"),
    }
)
_INCREMENTAL_COMPONENTS: Mapping[str, str] = MappingProxyType(
    {
        "M3-M2": "draft",
        "M4-M2": "rosh",
        "M5-M3": "rosh",
        "M5-M4": "draft",
        "M5-M2": "combined",
    }
)


def _validate_incremental_metrics(
    value: object,
    *,
    comparison: str,
) -> None:
    rows = _array(value, f"M6 comparison {comparison}.metrics")
    if not rows:
        raise ValueError(f"M6 comparison {comparison} has no paired metrics")
    seen: set[str] = set()
    significant = False
    for index, raw in enumerate(rows):
        row = _exact_object(
            raw,
            _PAIRED_METRIC_FIELDS,
            f"M6 comparison {comparison}.metrics[{index}]",
        )
        metric = row["metric"]
        if (
            not isinstance(metric, str)
            or metric not in {"brier_score", "log_loss"}
            or metric in seen
        ):
            raise ValueError(
                f"M6 comparison {comparison} has an invalid or duplicate metric"
            )
        seen.add(metric)
        delta = _optional_finite(
            row["delta"],
            f"M6 comparison {comparison}.{metric}.delta",
        )
        for confidence in ("ci_90", "ci_95"):
            interval = _exact_object(
                row[confidence],
                _BOOTSTRAP_INTERVAL_FIELDS,
                f"M6 comparison {comparison}.{metric}.{confidence}",
            )
            lower = _optional_finite(
                interval["lower"],
                f"M6 comparison {comparison}.{metric}.{confidence}.lower",
            )
            upper = _optional_finite(
                interval["upper"],
                f"M6 comparison {comparison}.{metric}.{confidence}.upper",
            )
            if (
                lower is not None
                and upper is not None
                and lower > upper
            ):
                raise ValueError(
                    f"M6 comparison {comparison}.{metric}.{confidence} is inverted"
                )
            if confidence == "ci_90" and delta is not None and upper is not None:
                significant = significant or (delta < 0.0 and upper < 0.0)
        probability = row["probability_of_improvement"]
        if probability is not None:
            _probability(
                probability,
                f"M6 comparison {comparison}.{metric}.probability_of_improvement",
            )
    if not significant:
        raise ValueError(
            f"M6 comparison {comparison} has no significant paired improvement"
        )


def _report_incremental_proof(
    report: Mapping[str, Any],
    *,
    model_kind: str,
) -> None:
    required = _REQUIRED_INCREMENTAL_COMPARISONS.get(model_kind)
    if required is None:
        raise ValueError("unsupported default model kind")
    if not required:
        return
    rows = report.get("incremental_comparisons")
    if not isinstance(rows, (list, tuple)):
        raise ValueError("M6 report lacks incremental comparison proof")
    by_name: dict[str, Mapping[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("M6 incremental proof rows must be objects")
        raw = _exact_object(raw, _INCREMENTAL_PROOF_FIELDS, "M6 incremental proof")
        name = raw.get("comparison")
        if (
            not isinstance(name, str)
            or name not in _INCREMENTAL_COMPONENTS
            or name in by_name
        ):
            raise ValueError("M6 incremental proof has duplicate comparison")
        by_name[name] = raw
    for name in required:
        row = by_name.get(name)
        if row is None:
            raise ValueError(f"M6 report lacks required comparison {name}")
        if row.get("added_component") != _INCREMENTAL_COMPONENTS[name]:
            raise ValueError(f"M6 comparison {name} has an invalid component")
        if row.get("status") != "incremental_value":
            raise ValueError(f"M6 comparison {name} lacks incremental value")
        support = row.get("available_support")
        if isinstance(support, bool) or not isinstance(support, int) or support < 20:
            raise ValueError(f"M6 comparison {name} lacks required support")
        reasons = row.get("reasons", ())
        if not isinstance(reasons, (list, tuple)) or reasons:
            raise ValueError(f"M6 comparison {name} carries rejection reasons")
        _validate_incremental_metrics(row["metrics"], comparison=name)


def _validate_report_identity(
    report: Mapping[str, Any],
    *,
    model: PrematchModelArtifact,
    calibration: PrematchCalibrationArtifact,
    mode: str,
) -> None:
    unknown_report_fields = frozenset(report) - _M6_REPORT_FIELDS
    if unknown_report_fields:
        raise ValueError(
            "M6 report has unknown fields: "
            + ", ".join(sorted(unknown_report_fields))
        )
    report_schema = report.get("schema")
    if report_schema is not None and report_schema not in {
        PREMATCH_DEPLOYMENT_PROOF_SCHEMA,
        PREMATCH_MODEL_RUN_METRICS_SCHEMA,
    }:
        raise ValueError("unsupported M6 report proof schema")
    report_mode = report.get("availability_mode")
    if report_mode is None:
        raise ValueError("M6 report lacks availability mode")
    if _mode(report_mode) != mode:
        raise ValueError("M6 report and deployment availability modes disagree")
    report_version = report.get("backtest_version")
    if report_version is None:
        raise ValueError("M6 report lacks backtest version")
    if report_version != PREMATCH_BACKTEST_VERSION:
        raise ValueError("M6 report version does not match")
    decision_kind, decision_status, _reasons = _report_default_decision(report)
    if decision_kind != model.model_kind:
        raise ValueError("M6 default model kind does not match deployment model")
    claimed_model_hash = report.get("model_hash")
    if (
        claimed_model_hash is not None
        and not hmac.compare_digest(
            _digest(claimed_model_hash, "M6 report model hash"),
            model.model_hash,
        )
    ):
        raise ValueError("M6 report model hash does not match deployment model")
    if mode == AvailabilityMode.RECONSTRUCTED.value:
        if decision_status != "reconstructed_only":
            raise ValueError(
                "reconstructed deployment requires reconstructed_only proof"
            )
    elif decision_status not in {"shadow_collecting", "passed"}:
        raise ValueError("prospective deployment has no valid default status")
    _report_calibration_proof(
        report,
        model_kind=model.model_kind,
        calibration=calibration,
    )
    _report_incremental_proof(report, model_kind=model.model_kind)


def _active_rosh_identity() -> tuple[str, str]:
    try:
        profile = get_profile()
    except Exception as error:  # pragma: no cover - profile source failure
        raise ValueError("active R.O.S.H. profile is unavailable") from error
    profile_hash = _digest(
        profile.canonical_profile_hash,
        "active R.O.S.H. profile hash",
    )
    formula = _nonempty(profile.formula_version, "active R.O.S.H. formula identity")
    return profile_hash, formula


def _validate_rosh_identity(
    snapshot: PrematchFeatureSnapshot,
    *,
    model_kind: str,
) -> None:
    if snapshot.rosh_status == "unavailable":
        if "rosh" in model_kind:
            raise ValueError(
                "R.O.S.H. model deployment requires available R.O.S.H. input"
            )
        return
    if snapshot.rosh_status != "available":
        raise ValueError("unsupported R.O.S.H. snapshot status")
    profile_hash, formula = _active_rosh_identity()
    if not hmac.compare_digest(snapshot.rosh_profile_hash, profile_hash):
        raise ValueError("R.O.S.H. profile hash does not match active profile")
    if snapshot.rosh_formula_version != formula:
        raise ValueError("R.O.S.H. formula version does not match active profile")
    if not hmac.compare_digest(
        snapshot.rosh_model_schema_hash,
        ROSH_MODEL_SCHEMA_HASH,
    ):
        raise ValueError("R.O.S.H. model schema does not match")
    if snapshot.rosh_input_hash is None:
        raise ValueError("R.O.S.H. input identity is incomplete")


def _invoke_stale_callback(
    callback: Callable[..., object],
    deployment: "FrozenPrematchDeployment",
) -> object:
    if not callable(callback):
        raise ValueError("stale_callback must be callable")
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(deployment)
    try:
        signature.bind(deployment)
    except TypeError:
        try:
            signature.bind()
        except TypeError as error:
            raise ValueError(
                "stale_callback must accept deployment or no arguments"
            ) from error
        return callback()
    return callback(deployment)


def _validate_dependency_identity(
    deployment: "FrozenPrematchDeployment",
    *,
    expected_dependency_fingerprint: str | None,
    expected_dependency_revision: int | None,
    current_dependency_fingerprint: str | None,
    current_dependency_revision: int | None,
    stale_callback: Callable[..., object] | None,
    require_current: bool,
) -> None:
    if expected_dependency_fingerprint is not None:
        expected_fingerprint = _digest(
            expected_dependency_fingerprint,
            "expected dependency fingerprint",
        )
        if not hmac.compare_digest(
            expected_fingerprint,
            deployment.dependency_fingerprint,
        ):
            raise RuntimeError("prematch deployment dependency fingerprint is stale")
    if expected_dependency_revision is not None:
        if (
            _positive_int(expected_dependency_revision, "expected dependency revision")
            != deployment.dependency_revision
        ):
            raise RuntimeError("prematch deployment dependency revision is stale")
    if current_dependency_fingerprint is not None:
        current_fingerprint = _digest(
            current_dependency_fingerprint,
            "current dependency fingerprint",
        )
        if not hmac.compare_digest(
            current_fingerprint,
            deployment.dependency_fingerprint,
        ):
            raise RuntimeError("prematch deployment dependency fingerprint is stale")
    if current_dependency_revision is not None:
        if (
            _positive_int(current_dependency_revision, "current dependency revision")
            != deployment.dependency_revision
        ):
            raise RuntimeError("prematch deployment dependency revision is stale")
    if require_current and (
        current_dependency_fingerprint is None
        and current_dependency_revision is None
        and stale_callback is None
    ):
        raise RuntimeError("runtime deployment requires a dependency freshness check")
    if stale_callback is None:
        return
    try:
        result = _invoke_stale_callback(stale_callback, deployment)
    except Exception as error:
        raise RuntimeError("prematch deployment stale check failed") from error
    if result is None or result is False:
        return
    if result is True:
        raise RuntimeError("prematch deployment dependency is stale")
    if isinstance(result, str) and result.strip():
        raise RuntimeError(f"prematch deployment dependency is stale: {result}")
    raise RuntimeError("prematch deployment stale check returned an invalid result")


@dataclass(frozen=True)
class FrozenPrematchDeployment:
    """Immutable M7 bundle for one exact prematch input snapshot."""

    deployment_key: str
    training_cutoff: datetime
    availability_mode: str
    dependency_fingerprint: str
    dependency_revision: int
    team_rating_artifact: TeamRatingArtifact
    feature_snapshot: PrematchFeatureSnapshot
    prematch_model_artifact: PrematchModelArtifact
    calibration_artifact: PrematchCalibrationArtifact
    m6_report_payload: Mapping[str, Any]
    m6_report_hash: str = ""

    def __post_init__(self) -> None:
        cutoff = _utc(self.training_cutoff, "training_cutoff")
        object.__setattr__(self, "training_cutoff", cutoff)
        object.__setattr__(self, "availability_mode", _mode(self.availability_mode))
        object.__setattr__(
            self,
            "dependency_fingerprint",
            _digest(self.dependency_fingerprint, "dependency_fingerprint"),
        )
        _positive_int(self.dependency_revision, "dependency_revision")
        for field, expected_type in (
            ("team_rating_artifact", TeamRatingArtifact),
            ("feature_snapshot", PrematchFeatureSnapshot),
            ("prematch_model_artifact", PrematchModelArtifact),
            ("calibration_artifact", PrematchCalibrationArtifact),
        ):
            if not isinstance(getattr(self, field), expected_type):
                raise ValueError(f"{field} has an unsupported type")
        payload = _report_payload(None, self.m6_report_payload)
        frozen_payload = _freeze(payload)
        if not isinstance(frozen_payload, Mapping):
            raise ValueError("M6 report payload must be an object")
        object.__setattr__(self, "m6_report_payload", frozen_payload)
        expected_report_hash = _hash(payload)
        if not self.m6_report_hash:
            object.__setattr__(self, "m6_report_hash", expected_report_hash)
        elif not hmac.compare_digest(
            _digest(self.m6_report_hash, "m6_report_hash"),
            expected_report_hash,
        ):
            raise ValueError("M6 report hash does not recompute")
        expected_key = _hash(self._payload_without_key())
        if not self.deployment_key:
            object.__setattr__(self, "deployment_key", expected_key)
        elif not hmac.compare_digest(
            _digest(self.deployment_key, "deployment_key"),
            expected_key,
        ):
            raise ValueError("prematch deployment key does not recompute")

    @property
    def prematch_feature_snapshot(self) -> PrematchFeatureSnapshot:
        """Compatibility alias used by callers that name the M5 type directly."""

        return self.feature_snapshot

    @property
    def model_artifact(self) -> PrematchModelArtifact:
        return self.prematch_model_artifact

    @property
    def calibration(self) -> PrematchCalibrationArtifact:
        return self.calibration_artifact

    @property
    def team_rating(self) -> TeamRatingArtifact:
        return self.team_rating_artifact

    @property
    def m6_report(self) -> Mapping[str, Any]:
        return self.m6_report_payload

    @property
    def default_decision_proof(self) -> Mapping[str, Any]:
        value = self.m6_report_payload.get("default_decision")
        if not isinstance(value, Mapping):
            raise ValueError("M6 report lacks default decision proof")
        return value

    @property
    def report_payload(self) -> Mapping[str, Any]:
        return self.m6_report_payload

    @property
    def report_hash(self) -> str:
        return self.m6_report_hash

    @property
    def model_kind(self) -> str:
        return self.prematch_model_artifact.model_kind

    @property
    def static_gate_authorized(self) -> bool:
        """Return static M6 gate state; lineage freshness is checked separately.

        Callers must invoke :func:`assert_frozen_prematch_deployment_deployable`
        with a current dependency revision/fingerprint before treating a bundle
        as runtime-authorized.
        """

        decision = self.m6_report_payload.get("default_decision")
        status = None if not isinstance(decision, Mapping) else decision.get("status")
        return (
            self.availability_mode == AvailabilityMode.PROSPECTIVE.value
            and status == "passed"
            and self.calibration_artifact.gate_passed
            and self.calibration_artifact.status is CalibrationStatus.PASSED
        )

    @property
    def evidence_mode(self) -> str:
        return self.availability_mode

    def _payload_without_key(self) -> dict[str, object]:
        return {
            "schema": PREMATCH_DEPLOYMENT_SCHEMA,
            "deployment_version": PREMATCH_DEPLOYMENT_VERSION,
            "training_cutoff": self.training_cutoff.isoformat(),
            "availability_mode": self.availability_mode,
            "dependency_fingerprint": self.dependency_fingerprint,
            "dependency_revision": self.dependency_revision,
            "team_rating_artifact": self.team_rating_artifact.to_payload(),
            "feature_snapshot": self.feature_snapshot.to_payload(),
            "prematch_model_artifact": self.prematch_model_artifact.to_payload(),
            "calibration_artifact": self.calibration_artifact.to_payload(),
            "m6_report": _thaw(self.m6_report_payload),
            "m6_report_hash": self.m6_report_hash,
        }

    def to_payload(self, *, include_deployment_key: bool = True) -> dict[str, object]:
        payload = self._payload_without_key()
        if include_deployment_key:
            payload["deployment_key"] = self.deployment_key
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    def to_identity_payload(self) -> dict[str, object]:
        return self._payload_without_key()


def _validate_bundle_identities(
    deployment: FrozenPrematchDeployment,
    *,
    for_runtime: bool,
) -> None:
    mode = deployment.availability_mode
    team_artifact = deployment.team_rating_artifact
    snapshot = deployment.feature_snapshot
    model = deployment.prematch_model_artifact
    calibration = deployment.calibration_artifact

    verify_team_rating_artifact(team_artifact)
    verify_prematch_feature_snapshot(snapshot)
    verify_prematch_model_artifact(model)
    replay_prematch_calibration_artifact(calibration)

    if snapshot.feature_version != PREMATCH_FEATURE_VERSION:
        raise ValueError("unsupported prematch feature version")
    if snapshot.availability_mode != mode:
        raise ValueError("feature snapshot and deployment modes disagree")
    if model.availability_mode != mode or calibration.availability_mode != mode:
        raise ValueError("artifact and deployment availability modes disagree")
    if model.status is not ModelStatus.TRAINED:
        raise ValueError("deployment model must be trained")
    if model.training_cutoff != deployment.training_cutoff:
        raise ValueError("model and deployment training cutoffs disagree")
    if calibration.calibration_cutoff != deployment.training_cutoff:
        raise ValueError("calibration and deployment training cutoffs disagree")
    if calibration.model_kind != model.model_kind:
        raise ValueError("model and calibration kinds disagree")
    if not calibration.gate_passed:
        raise ValueError("calibration gate did not pass")
    if calibration.parameters is None:
        raise ValueError("calibration artifact has no fitted parameters")
    if mode == AvailabilityMode.RECONSTRUCTED.value:
        if calibration.status is not CalibrationStatus.RECONSTRUCTED_ONLY:
            raise ValueError(
                "reconstructed deployment calibration must be reconstructed_only"
            )
    elif calibration.status is not CalibrationStatus.PASSED:
        raise ValueError("prospective deployment calibration is not passed")
    if calibration.calibration_cutoff >= snapshot.prediction_cutoff:
        raise ValueError("calibration was not usable before target prediction cutoff")
    if any(
        row.match_id == snapshot.match_id for row in model.training_corpus
    ):
        raise ValueError("target match entered prematch model training corpus")
    if any(
        row.match_id == snapshot.match_id for row in calibration.oos_samples
    ):
        raise ValueError("target match entered calibration evidence stream")
    if team_artifact.target.match_id != snapshot.match_id:
        raise ValueError("Team Rating and feature snapshot match IDs disagree")
    if team_artifact.prediction.match_id != snapshot.match_id:
        raise ValueError("Team Rating prediction and feature snapshot IDs disagree")
    if team_artifact.prediction.prediction_cutoff != snapshot.prediction_cutoff:
        raise ValueError("Team Rating and feature snapshot cutoffs disagree")
    if not hmac.compare_digest(
        team_artifact.artifact_hash,
        snapshot.team_rating_artifact_hash,
    ):
        raise ValueError("Team Rating artifact hash does not match feature snapshot")
    if (
        not hmac.compare_digest(
            team_artifact.prediction.input_hash,
            snapshot.team_rating_prediction_input_hash,
        )
    ):
        raise ValueError("Team Rating prediction hash does not match feature snapshot")
    if team_artifact.prediction.support != snapshot.team_rating_support:
        raise ValueError("Team Rating support does not match feature snapshot")
    expected_dependency = prematch_dependency_fingerprint(snapshot)
    if not hmac.compare_digest(
        deployment.dependency_fingerprint,
        expected_dependency,
    ):
        raise ValueError(
            "prematch deployment dependency fingerprint does not match feature snapshot"
        )
    probability = team_artifact.prediction.raw_probability
    expected_logit = math.log(probability) - math.log1p(-probability)
    if not math.isclose(
        expected_logit,
        snapshot.team_base_logit,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError("Team Rating offset does not match feature snapshot")
    if not hmac.compare_digest(
        model.feature_schema_hash,
        prematch_feature_schema_hash(model.model_kind),
    ):
        raise ValueError("prematch model feature schema does not match model kind")
    if model.training_cutoff > snapshot.prediction_cutoff:
        raise ValueError("model training cutoff follows target prediction cutoff")
    if not hmac.compare_digest(
        snapshot.draft_residual_feature_schema_hash,
        DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
    ):
        raise ValueError("Draft residual feature schema does not match")
    if not hmac.compare_digest(
        snapshot.draft_residual_model_schema_hash,
        DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
    ):
        raise ValueError("Draft residual model schema does not match")
    if not hmac.compare_digest(
        snapshot.rosh_model_schema_hash,
        ROSH_MODEL_SCHEMA_HASH,
    ):
        raise ValueError("R.O.S.H. model schema does not match")
    _validate_rosh_identity(snapshot, model_kind=model.model_kind)
    _validate_report_identity(
        deployment.m6_report_payload,
        model=model,
        calibration=calibration,
        mode=mode,
    )
    decision = deployment.m6_report_payload["default_decision"]
    decision_status = decision["status"]
    if for_runtime:
        if mode != AvailabilityMode.PROSPECTIVE.value:
            raise RuntimeError(
                "reconstructed prematch deployment cannot authorize prospective runtime"
            )
        if decision_status != "passed":
            raise RuntimeError("prematch deployment default gate is not passed")
        if calibration.status is not CalibrationStatus.PASSED:
            raise RuntimeError("prospective deployment calibration is not passed")


def verify_frozen_prematch_deployment(
    deployment: FrozenPrematchDeployment,
    *,
    expected_dependency_fingerprint: str | None = None,
    expected_dependency_revision: int | None = None,
    current_dependency_fingerprint: str | None = None,
    current_dependency_revision: int | None = None,
    stale_callback: Callable[..., object] | None = None,
    stale_check: Callable[..., object] | None = None,
    for_runtime: bool = False,
) -> None:
    """Replay and validate every identity in a frozen bundle."""

    if not isinstance(deployment, FrozenPrematchDeployment):
        raise ValueError("deployment must be a FrozenPrematchDeployment")
    if (
        stale_callback is not None
        and stale_check is not None
        and stale_callback is not stale_check
    ):
        raise ValueError("stale_callback and stale_check disagree")
    if stale_callback is None:
        stale_callback = stale_check
    claimed_deployment_key = _digest(
        deployment.deployment_key,
        "deployment_key",
    )
    if not hmac.compare_digest(
        claimed_deployment_key,
        _hash(deployment.to_payload(include_deployment_key=False)),
    ):
        raise ValueError("prematch deployment key does not recompute")
    claimed_report_hash = _digest(deployment.m6_report_hash, "m6_report_hash")
    if not hmac.compare_digest(
        claimed_report_hash,
        _hash(deployment.m6_report_payload),
    ):
        raise ValueError("M6 report hash does not recompute")
    _validate_bundle_identities(deployment, for_runtime=for_runtime)
    _validate_dependency_identity(
        deployment,
        expected_dependency_fingerprint=expected_dependency_fingerprint,
        expected_dependency_revision=expected_dependency_revision,
        current_dependency_fingerprint=current_dependency_fingerprint,
        current_dependency_revision=current_dependency_revision,
        stale_callback=stale_callback,
        require_current=for_runtime,
    )


def assert_frozen_prematch_deployment_deployable(
    deployment: FrozenPrematchDeployment,
    *,
    current_dependency_fingerprint: str | None = None,
    current_dependency_revision: int | None = None,
    stale_callback: Callable[..., object] | None = None,
    stale_check: Callable[..., object] | None = None,
) -> None:
    verify_frozen_prematch_deployment(
        deployment,
        current_dependency_fingerprint=current_dependency_fingerprint,
        current_dependency_revision=current_dependency_revision,
        stale_callback=stale_callback,
        stale_check=stale_check,
        for_runtime=True,
    )


def build_frozen_prematch_deployment(
    *,
    training_cutoff: datetime,
    availability_mode: AvailabilityMode | str,
    dependency_fingerprint: str | None = None,
    dependency_revision: int,
    team_rating_artifact: TeamRatingArtifact,
    feature_snapshot: PrematchFeatureSnapshot,
    prematch_model_artifact: PrematchModelArtifact,
    calibration_artifact: PrematchCalibrationArtifact,
    report: PrematchBacktestReport | Mapping[str, Any] | None = None,
    m6_report_payload: Mapping[str, Any] | None = None,
    m6_report_hash: str | None = None,
    expected_dependency_fingerprint: str | None = None,
    expected_dependency_revision: int | None = None,
    current_dependency_fingerprint: str | None = None,
    current_dependency_revision: int | None = None,
    stale_callback: Callable[..., object] | None = None,
    stale_check: Callable[..., object] | None = None,
    for_runtime: bool = False,
) -> FrozenPrematchDeployment:
    """Construct, hash, and strictly verify one frozen deployment bundle."""

    expected_dependency = prematch_dependency_fingerprint(feature_snapshot)
    if dependency_fingerprint is None:
        dependency_fingerprint = expected_dependency
    elif not hmac.compare_digest(
        _digest(dependency_fingerprint, "dependency_fingerprint"),
        expected_dependency,
    ):
        raise ValueError(
            "prematch deployment dependency fingerprint does not match feature snapshot"
        )
    payload = _report_payload(report, m6_report_payload)
    report_hash = _hash(payload)
    if m6_report_hash is not None and not hmac.compare_digest(
        _digest(m6_report_hash, "m6_report_hash"), report_hash
    ):
        raise ValueError("M6 report hash does not recompute")
    deployment = FrozenPrematchDeployment(
        deployment_key="",
        training_cutoff=training_cutoff,
        availability_mode=availability_mode,
        dependency_fingerprint=dependency_fingerprint,
        dependency_revision=dependency_revision,
        team_rating_artifact=team_rating_artifact,
        feature_snapshot=feature_snapshot,
        prematch_model_artifact=prematch_model_artifact,
        calibration_artifact=calibration_artifact,
        m6_report_payload=payload,
        m6_report_hash=report_hash,
    )
    verify_frozen_prematch_deployment(
        deployment,
        expected_dependency_fingerprint=expected_dependency_fingerprint,
        expected_dependency_revision=expected_dependency_revision,
        current_dependency_fingerprint=current_dependency_fingerprint,
        current_dependency_revision=current_dependency_revision,
        stale_callback=stale_callback,
        stale_check=stale_check,
        for_runtime=for_runtime,
    )
    return deployment


def frozen_prematch_deployment_from_payload(
    payload: Mapping[str, Any],
    *,
    expected_dependency_fingerprint: str | None = None,
    expected_dependency_revision: int | None = None,
    current_dependency_fingerprint: str | None = None,
    current_dependency_revision: int | None = None,
    stale_callback: Callable[..., object] | None = None,
    stale_check: Callable[..., object] | None = None,
    for_runtime: bool = False,
) -> FrozenPrematchDeployment:
    row = _exact_object(payload, _DEPLOYMENT_FIELDS, "prematch deployment")
    if row["schema"] != PREMATCH_DEPLOYMENT_SCHEMA:
        raise ValueError("unsupported prematch deployment schema")
    if row["deployment_version"] != PREMATCH_DEPLOYMENT_VERSION:
        raise ValueError("unsupported prematch deployment version")
    _mode(row["availability_mode"])
    _parse_utc(row["training_cutoff"], "training_cutoff")
    team_payload = row["team_rating_artifact"]
    model_payload = row["prematch_model_artifact"]
    calibration_payload = row["calibration_artifact"]
    team_artifact = team_rating_artifact_from_payload(team_payload)
    model = prematch_model_artifact_from_payload(model_payload)
    calibration = prematch_calibration_artifact_from_payload(calibration_payload)
    probability = team_artifact.prediction.raw_probability
    feature_snapshot = _feature_snapshot_from_payload(
        row["feature_snapshot"],
        team_base_logit=math.log(probability) - math.log1p(-probability),
    )
    report_payload = _report_payload(None, row["m6_report"])
    claimed_deployment_key = _digest(row["deployment_key"], "deployment_key")
    deployment = FrozenPrematchDeployment(
        # Let semantic validation report the first violated contract.  The
        # supplied key is compared immediately after the object recomputes it.
        deployment_key="",
        training_cutoff=_parse_utc(row["training_cutoff"], "training_cutoff"),
        availability_mode=_mode(row["availability_mode"]),
        dependency_fingerprint=_digest(
            row["dependency_fingerprint"], "dependency_fingerprint"
        ),
        dependency_revision=_positive_int(
            row["dependency_revision"], "dependency_revision"
        ),
        team_rating_artifact=team_artifact,
        feature_snapshot=feature_snapshot,
        prematch_model_artifact=model,
        calibration_artifact=calibration,
        m6_report_payload=report_payload,
        m6_report_hash=_digest(row["m6_report_hash"], "m6_report_hash"),
    )
    verify_frozen_prematch_deployment(
        deployment,
        expected_dependency_fingerprint=expected_dependency_fingerprint,
        expected_dependency_revision=expected_dependency_revision,
        current_dependency_fingerprint=current_dependency_fingerprint,
        current_dependency_revision=current_dependency_revision,
        stale_callback=stale_callback,
        stale_check=stale_check,
        for_runtime=for_runtime,
    )
    if not hmac.compare_digest(claimed_deployment_key, deployment.deployment_key):
        raise ValueError("prematch deployment key does not recompute")
    if canonical_json_bytes(deployment.to_payload()) != canonical_json_bytes(dict(row)):
        raise ValueError("prematch deployment payload is not canonical")
    return deployment


def load_frozen_prematch_deployment_json(
    payload_json: str,
    *,
    expected_dependency_fingerprint: str | None = None,
    expected_dependency_revision: int | None = None,
    current_dependency_fingerprint: str | None = None,
    current_dependency_revision: int | None = None,
    stale_callback: Callable[..., object] | None = None,
    stale_check: Callable[..., object] | None = None,
    for_runtime: bool = False,
) -> FrozenPrematchDeployment:
    return frozen_prematch_deployment_from_payload(
        _strict_json_object(payload_json),
        expected_dependency_fingerprint=expected_dependency_fingerprint,
        expected_dependency_revision=expected_dependency_revision,
        current_dependency_fingerprint=current_dependency_fingerprint,
        current_dependency_revision=current_dependency_revision,
        stale_callback=stale_callback,
        stale_check=stale_check,
        for_runtime=for_runtime,
    )


def load_prematch_deployment_json(
    payload_json: str,
    **kwargs: Any,
) -> FrozenPrematchDeployment:
    return load_frozen_prematch_deployment_json(payload_json, **kwargs)


def replay_frozen_prematch_deployment(
    deployment: FrozenPrematchDeployment | Mapping[str, Any] | str,
    *,
    expected_dependency_fingerprint: str | None = None,
    expected_dependency_revision: int | None = None,
    current_dependency_fingerprint: str | None = None,
    current_dependency_revision: int | None = None,
    stale_callback: Callable[..., object] | None = None,
    stale_check: Callable[..., object] | None = None,
    for_runtime: bool = False,
) -> FrozenPrematchDeployment:
    if isinstance(deployment, str):
        return load_frozen_prematch_deployment_json(
            deployment,
            expected_dependency_fingerprint=expected_dependency_fingerprint,
            expected_dependency_revision=expected_dependency_revision,
            current_dependency_fingerprint=current_dependency_fingerprint,
            current_dependency_revision=current_dependency_revision,
            stale_callback=stale_callback,
            stale_check=stale_check,
            for_runtime=for_runtime,
        )
    if isinstance(deployment, Mapping):
        return frozen_prematch_deployment_from_payload(
            deployment,
            expected_dependency_fingerprint=expected_dependency_fingerprint,
            expected_dependency_revision=expected_dependency_revision,
            current_dependency_fingerprint=current_dependency_fingerprint,
            current_dependency_revision=current_dependency_revision,
            stale_callback=stale_callback,
            stale_check=stale_check,
            for_runtime=for_runtime,
        )
    verify_frozen_prematch_deployment(
        deployment,
        expected_dependency_fingerprint=expected_dependency_fingerprint,
        expected_dependency_revision=expected_dependency_revision,
        current_dependency_fingerprint=current_dependency_fingerprint,
        current_dependency_revision=current_dependency_revision,
        stale_callback=stale_callback,
        stale_check=stale_check,
        for_runtime=for_runtime,
    )
    # Rehydrate through the strict payload loaders so replay covers every
    # nested canonical parser, not only the in-memory object validators.
    return frozen_prematch_deployment_from_payload(
        deployment.to_payload(),
        expected_dependency_fingerprint=expected_dependency_fingerprint,
        expected_dependency_revision=expected_dependency_revision,
        current_dependency_fingerprint=current_dependency_fingerprint,
        current_dependency_revision=current_dependency_revision,
        stale_callback=stale_callback,
        stale_check=stale_check,
        for_runtime=for_runtime,
    )


# Short aliases keep the public API consistent with the M2-M6 artifact modules.
build_prematch_deployment = build_frozen_prematch_deployment
verify_prematch_deployment = verify_frozen_prematch_deployment
replay_prematch_deployment = replay_frozen_prematch_deployment
prematch_deployment_from_payload = frozen_prematch_deployment_from_payload
assert_prematch_deployment_deployable = assert_frozen_prematch_deployment_deployable


__all__ = [
    "PREMATCH_DEPLOYMENT_PROOF_SCHEMA",
    "PREMATCH_DEPLOYMENT_SCHEMA",
    "PREMATCH_DEPLOYMENT_VERSION",
    "FrozenPrematchDeployment",
    "canonical_hash",
    "canonical_json_bytes",
    "assert_frozen_prematch_deployment_deployable",
    "assert_prematch_deployment_deployable",
    "build_frozen_prematch_deployment",
    "build_prematch_deployment",
    "frozen_prematch_deployment_from_payload",
    "load_frozen_prematch_deployment_json",
    "load_prematch_deployment_json",
    "prematch_deployment_from_payload",
    "replay_frozen_prematch_deployment",
    "replay_prematch_deployment",
    "verify_frozen_prematch_deployment",
    "verify_prematch_deployment",
]
