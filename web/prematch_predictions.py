from __future__ import annotations

import json
import logging
import math
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path as FilePath
from typing import Any, Iterator, Literal, Mapping

from fastapi import APIRouter, HTTPException, Path, Query
from sqlalchemy.exc import SQLAlchemyError

from database.session import PostgresSession
from event_intelligence.prematch_deployment import (
    FrozenPrematchDeployment,
    assert_frozen_prematch_deployment_deployable,
    load_frozen_prematch_deployment_json,
)
from event_intelligence.prematch_storage import (
    PREMATCH_VALIDATION_VERSION,
    current_prematch_lineage_revisions,
)

from . import queries


logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/intelligence/prematch",
    tags=["prematch-intelligence"],
)

ModelKind = Literal[
    "team_only",
    "team_plus_draft",
    "team_plus_rosh",
    "team_plus_draft_rosh",
    "team_plus_draft_rosh_clusters",
]
AvailabilityMode = Literal["reconstructed_walk_forward", "prospective"]
ModelStatus = Literal["trained", "insufficient_evidence"]
PredictionStatus = Literal[
    "predicted",
    "insufficient_evidence",
    "failed",
    "settled",
]

_CONTRIBUTION_FIELDS = (
    "feature_name",
    "component",
    "input_value",
    "standardized_value",
    "coefficient",
    "log_odds_contribution",
    "was_imputed",
)
_CALIBRATION_STATUSES = {
    "unsupported",
    "failed",
    "provisional",
    "reconstructed_only",
    "shadow_collecting",
    "passed",
}
_CLUSTER_IDS = tuple(f"C{index}" for index in range(10))
_CLUSTER_METADATA_FIELDS = frozenset(
    {
        "cluster_resource_version",
        "cluster_evidence_mode",
        "cluster_coverage",
        "cluster_support",
        "cluster_missing_reason",
        "cluster_counts",
        "cluster_assignments",
        "top_cluster_contributions",
    }
)
_CLUSTER_ASSIGNMENT_FIELDS = (
    "hero_id",
    "expected_role",
    "expected_lane",
    "cluster_id",
    "mapping_support",
    "mapping_confidence",
    "assignment_source",
    "missing_reason",
)
_RUNTIME_DEPLOYMENT_ENV = "PREMATCH_DEPLOYMENT_PATH"


class PrematchAuthorityError(RuntimeError):
    pass


@dataclass(frozen=True)
class _RuntimeDeploymentState:
    deployment: FrozenPrematchDeployment | None
    runtime_ready: bool
    runtime_block_reason: str | None


def _configured_runtime_deployment(
    connection: PostgresSession,
) -> _RuntimeDeploymentState:
    configured_path = os.environ.get(_RUNTIME_DEPLOYMENT_ENV, "").strip()
    if not configured_path:
        return _RuntimeDeploymentState(None, False, "deployment_not_configured")
    try:
        deployment = load_frozen_prematch_deployment_json(
            FilePath(configured_path).read_text(encoding="utf-8")
        )
    except (OSError, RuntimeError, ValueError):
        return _RuntimeDeploymentState(None, False, "deployment_invalid")

    dependency_revision, _ = current_prematch_lineage_revisions(connection)
    try:
        assert_frozen_prematch_deployment_deployable(
            deployment,
            current_dependency_revision=dependency_revision,
        )
    except (RuntimeError, ValueError) as error:
        return _RuntimeDeploymentState(deployment, False, str(error))
    return _RuntimeDeploymentState(deployment, True, None)


def _runtime_fields(
    state: _RuntimeDeploymentState,
    row: Mapping[str, Any],
    *,
    prediction: bool,
) -> dict[str, object]:
    deployment = state.deployment
    if deployment is None:
        return {
            "runtime_ready": False,
            "runtime_block_reason": state.runtime_block_reason,
            "deployment_key": None,
        }

    if row["model_hash"] != deployment.prematch_model_artifact.model_hash:
        reason = "deployment_model_mismatch"
    elif row["availability_mode"] != deployment.availability_mode:
        reason = "deployment_availability_mode_mismatch"
    elif _utc(row["training_cutoff"], "training cutoff") != (
        deployment.training_cutoff.isoformat()
    ):
        reason = "deployment_training_cutoff_mismatch"
    elif prediction and (
        row["calibration_hash"] != deployment.calibration_artifact.calibration_hash
    ):
        reason = "deployment_calibration_mismatch"
    elif prediction and row["match_id"] != deployment.feature_snapshot.match_id:
        reason = "deployment_match_mismatch"
    elif prediction and row["input_snapshot_hash"] != deployment.feature_snapshot.input_hash:
        reason = "deployment_input_snapshot_mismatch"
    elif prediction and row["dependency_fingerprint"] != deployment.dependency_fingerprint:
        reason = "deployment_dependency_fingerprint_mismatch"
    elif prediction and row["dependency_revision"] != deployment.dependency_revision:
        reason = "deployment_dependency_revision_mismatch"
    else:
        return {
            "runtime_ready": state.runtime_ready,
            "runtime_block_reason": state.runtime_block_reason,
            "deployment_key": deployment.deployment_key,
        }
    return {
        "runtime_ready": False,
        "runtime_block_reason": reason,
        "deployment_key": None,
    }


@contextmanager
def _database() -> Iterator[PostgresSession]:
    connection = queries.get_db()
    try:
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            yield connection
    finally:
        connection.close()


def _json_object(
    value: Any,
    field: str,
    *,
    optional: bool = False,
) -> dict[str, Any] | None:
    if value is None and optional:
        return None
    try:
        payload = dict(value) if isinstance(value, Mapping) else json.loads(value)
    except (TypeError, ValueError) as error:
        raise PrematchAuthorityError(f"invalid {field}") from error
    if not isinstance(payload, dict):
        raise PrematchAuthorityError(f"invalid {field}")
    return payload


def _utc(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except ValueError as error:
        raise PrematchAuthorityError(f"invalid {field}") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PrematchAuthorityError(f"non-UTC {field}")
    return parsed.astimezone(timezone.utc).isoformat()


def _pagination(page: int, page_size: int, total: int) -> dict[str, int]:
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
    }


def _filters(
    values: tuple[tuple[str, object | None], ...],
    *,
    required: str | None = None,
    required_params: tuple[object, ...] = (),
) -> tuple[str, tuple[object, ...]]:
    clauses = [] if required is None else [required]
    params = list(required_params)
    for column, value in values:
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    return (
        "" if not clauses else " WHERE " + " AND ".join(clauses),
        tuple(params),
    )


def _calibration(row: Mapping[str, Any]) -> dict[str, Any] | None:
    if row["calibration_hash"] is None:
        return None
    metrics = _json_object(row["calibration_metrics_json"], "calibration metrics")
    assert metrics is not None
    gate_passed = metrics.get("gate_passed")
    if not isinstance(gate_passed, bool):
        raise PrematchAuthorityError("invalid calibration gate proof")
    return {
        "calibration_hash": row["calibration_hash"],
        "calibration_version": row["calibration_version"],
        "fit_cutoff": _utc(
            row["calibration_fit_cutoff"], "fit cutoff", optional=True
        ),
        "evaluation_cutoff": _utc(
            row["calibration_evaluation_cutoff"], "evaluation cutoff"
        ),
        "fit_support": row["calibration_fit_support"],
        "evaluation_support": row["calibration_evaluation_support"],
        "parameters": _json_object(
            row["calibration_parameters_json"],
            "calibration parameters",
            optional=True,
        ),
        "metrics": metrics,
        "gate_passed": gate_passed,
        "status": row["calibration_status"],
        "created_at": _utc(row["calibration_created_at"], "calibration created_at"),
    }


def _model(
    row: Mapping[str, Any],
    runtime: _RuntimeDeploymentState,
) -> dict[str, Any]:
    if row["run_id"] != row["model_hash"]:
        raise PrematchAuthorityError("inconsistent prematch model identity")
    return {
        "run_id": row["run_id"],
        "model_hash": row["model_hash"],
        "model_version": row["model_version"],
        "artifact_version": row["artifact_version"],
        "model_kind": row["model_kind"],
        "availability_mode": row["availability_mode"],
        "training_cutoff": _utc(row["training_cutoff"], "training cutoff"),
        "feature_schema_hash": row["feature_schema_hash"],
        "training_input_hash": row["training_input_hash"],
        "metrics": _json_object(row["metrics_json"], "model metrics", optional=True),
        "status": row["status"],
        "created_at": _utc(row["created_at"], "model created_at"),
        "calibration": _calibration(row),
        **_runtime_fields(runtime, row, prediction=False),
    }


_MODEL_COLUMNS = """
    run.run_id, run.model_hash, run.model_version, run.artifact_version,
    run.model_kind, run.availability_mode, run.training_cutoff,
    run.feature_schema_hash, run.training_input_hash, run.metrics_json,
    run.status, run.created_at,
    calibration.calibration_hash, calibration.calibration_version,
    calibration.fit_cutoff AS calibration_fit_cutoff,
    calibration.evaluation_cutoff AS calibration_evaluation_cutoff,
    calibration.fit_support AS calibration_fit_support,
    calibration.evaluation_support AS calibration_evaluation_support,
    calibration.parameters_json AS calibration_parameters_json,
    calibration.metrics_json AS calibration_metrics_json,
    calibration.status AS calibration_status,
    calibration.created_at AS calibration_created_at
"""


def list_models(
    *,
    page: int,
    page_size: int,
    model_kind: ModelKind | None,
    availability_mode: AvailabilityMode | None,
    status: ModelStatus | None,
) -> dict[str, object]:
    where, params = _filters(
        (
            ("run.model_kind", model_kind),
            ("run.availability_mode", availability_mode),
            ("run.status", status),
        )
    )
    with _database() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS total FROM prematch_model_runs AS run" + where,
            params,
        ).fetchone()
        if count is None:
            raise PrematchAuthorityError("model count is unavailable")
        rows = connection.execute(
            f"""SELECT {_MODEL_COLUMNS}
                  FROM prematch_model_runs AS run
                  LEFT JOIN LATERAL (
                      SELECT calibration_hash, calibration_version, fit_cutoff,
                             evaluation_cutoff, fit_support, evaluation_support,
                             parameters_json, metrics_json, status, created_at
                        FROM prematch_calibration_artifacts
                       WHERE model_hash=run.model_hash
                       ORDER BY evaluation_cutoff DESC, calibration_hash DESC
                       LIMIT 1
                  ) AS calibration ON TRUE
                  {where}
                 ORDER BY run.training_cutoff DESC, run.model_kind,
                          run.availability_mode, run.run_id
                 LIMIT ? OFFSET ?""",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        runtime = _configured_runtime_deployment(connection)
    total = int(count["total"])
    return {
        "data": [_model(row, runtime) for row in rows],
        "pagination": _pagination(page, page_size, total),
    }


_CURRENT_PREDICTION = """
    validation.input_snapshot_hash=prediction.input_snapshot_hash
    AND validation.artifact_fingerprint=prediction.artifact_fingerprint
    AND validation.dependency_fingerprint=prediction.dependency_fingerprint
    AND validation.dependency_revision=prediction.dependency_revision
    AND validation.validation_version=?
    AND prematch_lineage_revision_is_current(
        validation.dependency_revision, prediction.prediction_cutoff
    )
"""
_PREDICTION_FROM = """
    FROM prematch_predictions AS prediction
    JOIN prematch_model_runs AS run ON run.run_id=prediction.run_id
    JOIN prematch_prediction_validations AS validation
      ON validation.run_id=prediction.run_id
     AND validation.match_id=prediction.match_id
    LEFT JOIN prematch_calibration_artifacts AS calibration
      ON calibration.calibration_hash=prediction.calibration_hash
"""
_PREDICTION_COLUMNS = """
    prediction.run_id, run.model_hash, run.model_kind,
    run.status AS model_status, run.availability_mode, run.training_cutoff,
    prediction.match_id, prediction.prediction_cutoff,
    prediction.cutoff_source, prediction.input_snapshot_hash,
    prediction.artifact_fingerprint, prediction.dependency_fingerprint,
    prediction.dependency_revision, prediction.calibration_hash,
    prediction.team_base_probability, prediction.raw_probability,
    prediction.calibrated_probability, prediction.parameter_uncertainty,
    prediction.draft_logit_delta, prediction.rosh_logit_delta,
    prediction.cluster_logit_delta, prediction.total_adjustment,
    prediction.coverage, prediction.support, prediction.prediction_json,
    prediction.eventual_radiant_win, prediction.result_usable_at,
    prediction.settled_at, prediction.status,
    validation.validation_version, validation.validated_at,
    calibration.calibration_hash AS calibration_authority_hash,
    calibration.status AS calibration_status,
    calibration.evaluation_support AS calibration_evaluation_support,
    calibration.metrics_json AS calibration_metrics_json
"""


def _prediction_filters(
    *,
    model_kind: ModelKind | None,
    availability_mode: AvailabilityMode | None,
    status: PredictionStatus | None,
    match_id: int | None = None,
) -> tuple[str, tuple[object, ...]]:
    return _filters(
        (
            ("run.model_kind", model_kind),
            ("run.availability_mode", availability_mode),
            ("prediction.status", status),
            ("prediction.match_id", match_id),
        ),
        required=_CURRENT_PREDICTION,
        required_params=(PREMATCH_VALIDATION_VERSION,),
    )


def _top_contributions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PrematchAuthorityError("invalid top_contributions")
    output = []
    for item in value:
        if not isinstance(item, dict) or any(
            field not in item for field in _CONTRIBUTION_FIELDS
        ):
            raise PrematchAuthorityError("invalid top_contributions")
        output.append({field: item[field] for field in _CONTRIBUTION_FIELDS})
    return output


def _cluster_counts(value: Any) -> dict[str, dict[str, float]]:
    if not isinstance(value, Mapping) or set(value) != set(_CLUSTER_IDS):
        raise PrematchAuthorityError("invalid cluster_counts")
    output = {}
    for cluster_id in _CLUSTER_IDS:
        counts = value[cluster_id]
        if not isinstance(counts, Mapping) or set(counts) != {
            "radiant",
            "dire",
            "difference",
        }:
            raise PrematchAuthorityError("invalid cluster_counts")
        normalized = {}
        for field in ("radiant", "dire", "difference"):
            count = counts[field]
            if (
                isinstance(count, bool)
                or not isinstance(count, (int, float))
                or not math.isfinite(count)
            ):
                raise PrematchAuthorityError("invalid cluster_counts")
            normalized[field] = float(count)
        if normalized["radiant"] < 0.0 or normalized["dire"] < 0.0:
            raise PrematchAuthorityError("invalid cluster_counts")
        output[cluster_id] = normalized
    return output


def _cluster_assignments(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, Mapping) or set(value) != {"radiant", "dire"}:
        raise PrematchAuthorityError("invalid cluster_assignments")
    output = {}
    for side in ("radiant", "dire"):
        assignments = value[side]
        if not isinstance(assignments, list) or len(assignments) != 5:
            raise PrematchAuthorityError("invalid cluster_assignments")
        normalized = []
        for assignment in assignments:
            if not isinstance(assignment, Mapping) or any(
                field not in assignment for field in _CLUSTER_ASSIGNMENT_FIELDS
            ):
                raise PrematchAuthorityError("invalid cluster_assignments")
            hero_id = assignment["hero_id"]
            support = assignment["mapping_support"]
            confidence = assignment["mapping_confidence"]
            if (
                isinstance(hero_id, bool)
                or not isinstance(hero_id, int)
                or hero_id <= 0
                or isinstance(support, bool)
                or not isinstance(support, int)
                or support < 0
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence)
                or not 0.0 <= confidence <= 1.0
            ):
                raise PrematchAuthorityError("invalid cluster_assignments")
            if any(
                assignment[field] is not None
                and not isinstance(assignment[field], str)
                for field in ("expected_role", "expected_lane", "missing_reason")
            ) or any(
                not isinstance(assignment[field], str) or not assignment[field]
                for field in ("cluster_id", "assignment_source")
            ) or assignment["cluster_id"] not in {*_CLUSTER_IDS, "unavailable"}:
                raise PrematchAuthorityError("invalid cluster_assignments")
            normalized.append(
                {field: assignment[field] for field in _CLUSTER_ASSIGNMENT_FIELDS}
            )
        output[side] = normalized
    return output


def _cluster_metadata(artifact: Mapping[str, Any]) -> dict[str, object]:
    present = _CLUSTER_METADATA_FIELDS.intersection(artifact)
    if not present:
        return {
            "cluster_coverage": 0.0,
            "cluster_support": 0,
            "cluster_resource_version": None,
            "cluster_evidence_mode": None,
            "cluster_missing_reason": "cluster_evidence_unavailable",
            "cluster_counts": {},
            "cluster_assignments": {"radiant": [], "dire": []},
            "top_cluster_contributions": [],
        }
    if present != _CLUSTER_METADATA_FIELDS:
        raise PrematchAuthorityError("incomplete cluster metadata")

    resource_version = artifact["cluster_resource_version"]
    evidence_mode = artifact["cluster_evidence_mode"]
    coverage = artifact["cluster_coverage"]
    support = artifact["cluster_support"]
    missing_reason = artifact["cluster_missing_reason"]
    if not isinstance(resource_version, str) or not resource_version:
        raise PrematchAuthorityError("invalid cluster resource version")
    if evidence_mode not in {"published_static", "reconstructed_walk_forward"}:
        raise PrematchAuthorityError("invalid cluster evidence mode")
    if (
        isinstance(coverage, bool)
        or not isinstance(coverage, (int, float))
        or not math.isfinite(coverage)
        or not 0.0 <= coverage <= 1.0
    ):
        raise PrematchAuthorityError("invalid cluster coverage")
    if isinstance(support, bool) or not isinstance(support, int) or support < 0:
        raise PrematchAuthorityError("invalid cluster support")
    if missing_reason is not None and (
        not isinstance(missing_reason, str) or not missing_reason
    ):
        raise PrematchAuthorityError("invalid cluster missing reason")
    contributions = _top_contributions(artifact["top_cluster_contributions"])
    if any(row["component"] != "cluster" for row in contributions):
        raise PrematchAuthorityError("invalid top_cluster_contributions")
    return {
        "cluster_coverage": float(coverage),
        "cluster_support": support,
        "cluster_resource_version": resource_version,
        "cluster_evidence_mode": evidence_mode,
        "cluster_missing_reason": missing_reason,
        "cluster_counts": _cluster_counts(artifact["cluster_counts"]),
        "cluster_assignments": _cluster_assignments(artifact["cluster_assignments"]),
        "top_cluster_contributions": contributions,
    }


def _prediction(
    row: Mapping[str, Any],
    runtime: _RuntimeDeploymentState,
) -> dict[str, object]:
    if row["run_id"] != row["model_hash"]:
        raise PrematchAuthorityError("inconsistent prediction model identity")
    artifact = _json_object(row["prediction_json"], "prediction artifact")
    assert artifact is not None
    stored_status = str(row["status"])
    artifact_status = "predicted" if stored_status == "settled" else stored_status
    claims = {
        "status": artifact_status,
        "model_hash": row["run_id"],
        "input_snapshot_hash": row["input_snapshot_hash"],
        "support": row["support"],
        "raw_probability": row["raw_probability"],
        "parameter_uncertainty": row["parameter_uncertainty"],
        "draft_logit_delta": row["draft_logit_delta"],
        "rosh_logit_delta": row["rosh_logit_delta"],
        "cluster_logit_delta": row["cluster_logit_delta"],
        "total_adjustment": row["total_adjustment"],
    }
    if any(artifact.get(key) != value for key, value in claims.items()):
        raise PrematchAuthorityError("prediction artifact claim mismatch")
    calibrated = row["calibrated_probability"]
    if (row["calibration_hash"] is None) != (calibrated is None):
        raise PrematchAuthorityError("calibration identity mismatch")
    calibration_status = None
    if row["calibration_hash"] is not None:
        if row["calibration_authority_hash"] != row["calibration_hash"]:
            raise PrematchAuthorityError("calibration authority is unavailable")
        calibration_status = str(row["calibration_status"])
        calibration_metrics = _json_object(
            row["calibration_metrics_json"],
            "calibration metrics",
        )
        assert calibration_metrics is not None
        gate_passed = calibration_metrics.get("gate_passed")
        evaluation_support = row["calibration_evaluation_support"]
        if (
            calibration_status not in _CALIBRATION_STATUSES
            or not isinstance(gate_passed, bool)
            or isinstance(evaluation_support, bool)
            or not isinstance(evaluation_support, int)
            or evaluation_support < 0
        ):
            raise PrematchAuthorityError("invalid calibration authority")
    missing = artifact.get("missing_features")
    if not isinstance(missing, list) or not all(
        isinstance(value, str) for value in missing
    ):
        raise PrematchAuthorityError("invalid missing_features")
    result = row["eventual_radiant_win"]
    if result not in (None, 0, 1):
        raise PrematchAuthorityError("invalid settlement result")
    validation_version = str(row["validation_version"])
    if validation_version != PREMATCH_VALIDATION_VERSION:
        raise PrematchAuthorityError("unsupported validation version")
    team_base_logit = artifact.get("team_base_logit")
    if not isinstance(team_base_logit, (int, float)) or isinstance(
        team_base_logit, bool
    ):
        raise PrematchAuthorityError("invalid team base logit")
    team_base_probability = float(row["team_base_probability"])
    sigmoid = (
        1.0 / (1.0 + math.exp(-team_base_logit))
        if team_base_logit >= 0.0
        else math.exp(team_base_logit) / (1.0 + math.exp(team_base_logit))
    )
    if not math.isclose(
        sigmoid,
        team_base_probability,
        rel_tol=0.0,
        abs_tol=1e-15,
    ):
        raise PrematchAuthorityError("team base probability mismatch")
    reason = artifact.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise PrematchAuthorityError("invalid prediction reason")
    cluster_metadata = _cluster_metadata(artifact)
    cluster_candidate_delta = artifact.get("cluster_candidate_logit_delta")
    if cluster_candidate_delta is not None and (
        isinstance(cluster_candidate_delta, bool)
        or not isinstance(cluster_candidate_delta, (int, float))
        or not math.isfinite(cluster_candidate_delta)
        or cluster_metadata["cluster_resource_version"] is None
    ):
        raise PrematchAuthorityError("invalid cluster candidate logit delta")
    cluster_analysis_delta = (
        row["cluster_logit_delta"]
        if row["cluster_logit_delta"] is not None
        else cluster_candidate_delta
    )
    if (
        row["model_kind"] == "team_plus_draft_rosh_clusters"
        and stored_status in {"predicted", "settled"}
        and (
            cluster_metadata["cluster_resource_version"] is None
            or row["cluster_logit_delta"] is None
        )
    ):
        raise PrematchAuthorityError("cluster prediction metadata is unavailable")
    return {
        "run_id": row["run_id"],
        "model_hash": row["model_hash"],
        "model_kind": row["model_kind"],
        "model_status": row["model_status"],
        "availability_mode": row["availability_mode"],
        "training_cutoff": _utc(row["training_cutoff"], "training cutoff"),
        "match_id": row["match_id"],
        "prediction_cutoff": _utc(row["prediction_cutoff"], "prediction cutoff"),
        "cutoff_source": row["cutoff_source"],
        "input_snapshot_hash": row["input_snapshot_hash"],
        "artifact_fingerprint": row["artifact_fingerprint"],
        "dependency_fingerprint": row["dependency_fingerprint"],
        "dependency_revision": row["dependency_revision"],
        "calibration_hash": row["calibration_hash"],
        "calibration_status": calibration_status,
        "team_base_probability": team_base_probability,
        "raw_probability": row["raw_probability"],
        "calibrated_probability": calibrated,
        "parameter_uncertainty": row["parameter_uncertainty"],
        "draft_logit_delta": row["draft_logit_delta"],
        "rosh_logit_delta": row["rosh_logit_delta"],
        "cluster_logit_delta": cluster_analysis_delta,
        "total_adjustment": row["total_adjustment"],
        "coverage": row["coverage"],
        "support": row["support"],
        "eventual_radiant_win": None if result is None else bool(result),
        "result_usable_at": _utc(
            row["result_usable_at"], "result usable_at", optional=True
        ),
        "settled_at": _utc(row["settled_at"], "settled_at", optional=True),
        "status": stored_status,
        "reason": reason,
        "learned_intercept": artifact.get("learned_intercept"),
        "missing_features": missing,
        "top_contributions": _top_contributions(artifact.get("top_contributions")),
        **cluster_metadata,
        "validation": {
            "validation_version": validation_version,
            "validated_at": _utc(row["validated_at"], "validated_at"),
        },
        **_runtime_fields(runtime, row, prediction=True),
    }


def _prediction_rows(
    connection: PostgresSession,
    *,
    model_kind: ModelKind | None = None,
    availability_mode: AvailabilityMode | None = None,
    status: PredictionStatus | None = None,
    match_id: int | None = None,
    limit: tuple[int, int] | None = None,
) -> list[Mapping[str, Any]]:
    where, params = _prediction_filters(
        model_kind=model_kind,
        availability_mode=availability_mode,
        status=status,
        match_id=match_id,
    )
    pagination_sql = ""
    if limit is not None:
        pagination_sql = " LIMIT ? OFFSET ?"
        params = (*params, *limit)
    return connection.execute(
        f"""SELECT {_PREDICTION_COLUMNS} {_PREDICTION_FROM} {where}
             ORDER BY prediction.prediction_cutoff DESC,
                      prediction.match_id DESC, run.model_kind, run.run_id
             {pagination_sql}""",
        params,
    ).fetchall()


def list_predictions(
    *,
    page: int,
    page_size: int,
    model_kind: ModelKind | None,
    availability_mode: AvailabilityMode | None,
    status: PredictionStatus | None,
) -> dict[str, object]:
    where, params = _prediction_filters(
        model_kind=model_kind,
        availability_mode=availability_mode,
        status=status,
    )
    with _database() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS total " + _PREDICTION_FROM + where,
            params,
        ).fetchone()
        if count is None:
            raise PrematchAuthorityError("prediction count is unavailable")
        rows = _prediction_rows(
            connection,
            model_kind=model_kind,
            availability_mode=availability_mode,
            status=status,
            limit=(page_size, (page - 1) * page_size),
        )
        runtime = _configured_runtime_deployment(connection)
    total = int(count["total"])
    return {
        "data": [_prediction(row, runtime) for row in rows],
        "pagination": _pagination(page, page_size, total),
    }


def get_match_predictions(match_id: int) -> dict[str, object] | None:
    with _database() as connection:
        exists = connection.execute(
            "SELECT 1 AS present FROM match_ingest_status WHERE match_id=?",
            (match_id,),
        ).fetchone()
        if exists is None:
            return None
        rows = _prediction_rows(connection, match_id=match_id)
        runtime = _configured_runtime_deployment(connection)
    return {
        "match_id": match_id,
        "predictions": [_prediction(row, runtime) for row in rows],
    }


def _unavailable(error: BaseException) -> HTTPException:
    logger.exception("Prematch PostgreSQL authority is unavailable")
    return HTTPException(
        status_code=503,
        detail="Prematch prediction authority is unavailable",
    )


@router.get("/models")
def models(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    model_kind: ModelKind | None = None,
    availability_mode: AvailabilityMode | None = None,
    status: ModelStatus | None = None,
):
    try:
        return list_models(
            page=page,
            page_size=page_size,
            model_kind=model_kind,
            availability_mode=availability_mode,
            status=status,
        )
    except (KeyError, TypeError, ValueError, PrematchAuthorityError, SQLAlchemyError) as error:
        raise _unavailable(error) from error


@router.get("/predictions")
def predictions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    model_kind: ModelKind | None = None,
    availability_mode: AvailabilityMode | None = None,
    status: PredictionStatus | None = None,
):
    try:
        return list_predictions(
            page=page,
            page_size=page_size,
            model_kind=model_kind,
            availability_mode=availability_mode,
            status=status,
        )
    except (KeyError, TypeError, ValueError, PrematchAuthorityError, SQLAlchemyError) as error:
        raise _unavailable(error) from error


@router.get("/matches/{match_id}")
def match_predictions(match_id: int = Path(..., gt=0)):
    try:
        result = get_match_predictions(match_id)
    except (KeyError, TypeError, ValueError, PrematchAuthorityError, SQLAlchemyError) as error:
        raise _unavailable(error) from error
    if result is None:
        raise HTTPException(status_code=404, detail="Prematch match not found")
    return result


__all__ = ["get_match_predictions", "list_models", "list_predictions", "router"]
