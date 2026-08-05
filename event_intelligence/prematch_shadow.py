"""Prospective prematch shadow collection, settlement, and health metrics."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from database.session import PostgresSession
from live_betting.health import record_health

from .draft_features import AvailabilityMode
from .draft_model import evaluate_binary_predictions
from .prematch_deployment import (
    FrozenPrematchDeployment,
    verify_frozen_prematch_deployment,
)
from .prematch_storage import (
    PrematchPersistenceCounts,
    PrematchPredictionRecord,
    build_prematch_calibration_record,
    build_prematch_model_run_record,
    build_prematch_prediction_record,
    build_prematch_validation_record,
    current_prematch_lineage_revisions,
    persist_prematch_records,
    settle_prematch_prediction,
)


UTC = timezone.utc
PREMATCH_SHADOW_VERSION = "prematch-prospective-shadow-v1"
PREMATCH_SHADOW_COMPONENTS = (
    "prematch_team_rating",
    "prematch_draft_residual",
    "prematch_rosh",
    "prematch_prediction",
    "prematch_settlement",
)
PROSPECTIVE_MIN_SETTLED_MAPS = 200
PROSPECTIVE_MIN_EVENTS = 5
PROSPECTIVE_MIN_PATCHES = 2
PROSPECTIVE_MAX_EVENT_SHARE = 0.40
ROLLING_METRIC_WINDOW = 200


@dataclass(frozen=True)
class PrematchShadowCollection:
    prediction: PrematchPredictionRecord
    persistence: PrematchPersistenceCounts


@dataclass(frozen=True)
class PrematchShadowSettlement:
    eligible: int
    updated: int
    unchanged: int


@dataclass(frozen=True)
class PrematchShadowMetrics:
    prediction_support: int
    settled_support: int
    metric_support: int
    formal_events: int
    patches: int
    single_event_share: float | None
    mean_coverage: float | None
    missing_rosh_rate: float | None
    stale_deployment_rate: float | None
    mean_settlement_delay_seconds: float | None
    brier_score: float | None
    log_loss: float | None
    calibration_drift_ece: float | None


@dataclass(frozen=True)
class PrematchProspectiveDecision:
    status: str
    reasons: tuple[str, ...]


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
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


def collect_prematch_shadow(
    connection: PostgresSession,
    deployment: FrozenPrematchDeployment,
    *,
    observed_at: datetime,
    cutoff_source: str = "prospective_draft_complete",
) -> PrematchShadowCollection:
    """Persist one prospective prediction without creating an order."""

    observed = _utc(observed_at, "observed_at")
    if deployment.availability_mode != AvailabilityMode.PROSPECTIVE.value:
        raise ValueError("prematch shadow requires a prospective deployment")
    if observed < deployment.feature_snapshot.prediction_cutoff:
        raise ValueError("shadow observation precedes prediction cutoff")
    dependency_revision, _artifact_revision = current_prematch_lineage_revisions(
        connection
    )
    verify_frozen_prematch_deployment(
        deployment,
        current_dependency_revision=dependency_revision,
    )
    model = build_prematch_model_run_record(deployment.prematch_model_artifact)
    calibration = build_prematch_calibration_record(
        deployment.calibration_artifact,
        model_hash=model.model_hash,
    )
    prediction = build_prematch_prediction_record(
        deployment.prematch_model_artifact,
        deployment.feature_snapshot,
        cutoff_source=cutoff_source,
        dependency_fingerprint=deployment.dependency_fingerprint,
        dependency_revision=dependency_revision,
        calibration=calibration,
    )
    validation = build_prematch_validation_record(
        model,
        prediction,
        validated_at=observed,
    )
    persistence = persist_prematch_records(
        connection,
        model_runs=(model,),
        calibration_artifacts=(calibration,),
        predictions=(prediction,),
        validations=(validation,),
        created_at=observed,
    )
    _record_collection_health(connection, deployment, prediction, observed)
    return PrematchShadowCollection(prediction, persistence)


def _record_collection_health(
    connection: PostgresSession,
    deployment: FrozenPrematchDeployment,
    prediction: PrematchPredictionRecord,
    observed_at: datetime,
) -> None:
    statuses = {
        "prematch_team_rating": (
            "healthy",
            {"support": deployment.feature_snapshot.team_rating_support},
        ),
        "prematch_draft_residual": (
            "healthy" if deployment.feature_snapshot.draft_coverage > 0.0 else "degraded",
            {"coverage": deployment.feature_snapshot.draft_coverage},
        ),
        "prematch_rosh": (
            "healthy" if deployment.feature_snapshot.rosh_status == "available" else "degraded",
            {
                "status": deployment.feature_snapshot.rosh_status,
                "missing_reason": deployment.feature_snapshot.rosh_missing_reason,
            },
        ),
        "prematch_prediction": (
            "healthy" if prediction.status == "predicted" else "degraded",
            {"status": prediction.status, "coverage": prediction.coverage},
        ),
    }
    for component, (status, details) in statuses.items():
        record_health(
            connection,
            component,
            status,
            heartbeat_at=observed_at,
            success_at=observed_at if status == "healthy" else None,
            details={"shadow_version": PREMATCH_SHADOW_VERSION, **details},
        )


def settle_ready_prematch_shadows(
    connection: PostgresSession,
    *,
    observed_at: datetime,
) -> PrematchShadowSettlement:
    """Settle pending shadow rows only after an authoritative result is usable."""

    observed = _utc(observed_at, "observed_at")
    rows = connection.execute(
        """SELECT prediction.run_id, prediction.match_id,
                  match.radiant_win, source.first_usable_at
             FROM prematch_predictions AS prediction
             JOIN prematch_model_runs AS run USING(run_id)
             JOIN matches AS match USING(match_id)
             JOIN match_ingest_status AS ingest USING(match_id)
             JOIN raw_source_artifacts AS source
               ON source.artifact_id=ingest.latest_raw_artifact_id
              AND source.content_hash=ingest.latest_raw_content_hash
            WHERE run.availability_mode='prospective'
              AND prediction.status='predicted'
              AND match.radiant_win IS NOT NULL
              AND source.first_usable_at IS NOT NULL
              AND live_text_timestamp_utc(source.first_usable_at)
                  <= live_text_timestamp_utc(?)
            ORDER BY source.first_usable_at, prediction.match_id, prediction.run_id""",
        (observed.isoformat(),),
    ).fetchall()
    updated = 0
    unchanged = 0
    for row in rows:
        result = settle_prematch_prediction(
            connection,
            run_id=str(row["run_id"]),
            match_id=int(row["match_id"]),
            eventual_radiant_win=bool(row["radiant_win"]),
            result_usable_at=_parse_utc(
                row["first_usable_at"],
                "result first_usable_at",
            ),
            settled_at=observed,
        )
        updated += int(result.updated)
        unchanged += int(result.unchanged)
    record_health(
        connection,
        "prematch_settlement",
        "healthy",
        heartbeat_at=observed,
        success_at=observed,
        details={
            "shadow_version": PREMATCH_SHADOW_VERSION,
            "eligible": len(rows),
            "updated": updated,
            "unchanged": unchanged,
        },
    )
    return PrematchShadowSettlement(len(rows), updated, unchanged)


def load_prematch_shadow_metrics(
    connection: PostgresSession,
) -> PrematchShadowMetrics:
    rows = connection.execute(
        """SELECT prediction.raw_probability,
                  prediction.calibrated_probability,
                  prediction.coverage, prediction.rosh_logit_delta,
                  prediction.prediction_json,
                  prediction.eventual_radiant_win,
                  prediction.result_usable_at, prediction.settled_at,
                  prediction.status, ingest.event_id, match.patch,
                  prematch_lineage_revision_is_current(
                      validation.dependency_revision,
                      prediction.prediction_cutoff
                  ) AS is_current
             FROM prematch_predictions AS prediction
             JOIN prematch_model_runs AS run USING(run_id)
             JOIN prematch_prediction_validations AS validation
               ON validation.run_id=prediction.run_id
              AND validation.match_id=prediction.match_id
             LEFT JOIN match_ingest_status AS ingest USING(match_id)
             LEFT JOIN matches AS match USING(match_id)
            WHERE run.availability_mode='prospective'
            ORDER BY prediction.prediction_cutoff DESC,
                     prediction.match_id DESC, prediction.run_id"""
    ).fetchall()
    prediction_support = len(rows)
    settled = [row for row in rows if str(row["status"]) == "settled"]
    metric_rows = settled[:ROLLING_METRIC_WINDOW]
    probabilities: list[float] = []
    outcomes: list[bool] = []
    for row in metric_rows:
        probability = row["calibrated_probability"]
        if probability is None:
            probability = row["raw_probability"]
        if probability is None or row["eventual_radiant_win"] is None:
            continue
        probabilities.append(float(probability))
        outcomes.append(bool(row["eventual_radiant_win"]))
    binary = evaluate_binary_predictions(outcomes, probabilities, ece_bins=5)
    events = Counter(
        str(row["event_id"])
        for row in settled
        if row["event_id"] is not None and str(row["event_id"]).strip()
    )
    patches = {
        str(row["patch"])
        for row in settled
        if row["patch"] is not None and str(row["patch"]).strip()
    }
    delays = [
        (
            _parse_utc(row["settled_at"], "settled_at")
            - _parse_utc(row["result_usable_at"], "result_usable_at")
        ).total_seconds()
        for row in settled
    ]
    missing_rosh = 0
    for row in rows:
        payload = json.loads(str(row["prediction_json"]))
        missing = payload.get("missing_features", [])
        if row["rosh_logit_delta"] is None or "relative_advantage" in missing:
            missing_rosh += 1
    return PrematchShadowMetrics(
        prediction_support=prediction_support,
        settled_support=len(settled),
        metric_support=binary.support,
        formal_events=len(events),
        patches=len(patches),
        single_event_share=(
            None if not settled or not events else max(events.values()) / len(settled)
        ),
        mean_coverage=(
            None
            if not rows
            else math.fsum(float(row["coverage"]) for row in rows) / len(rows)
        ),
        missing_rosh_rate=(
            None if not rows else missing_rosh / len(rows)
        ),
        stale_deployment_rate=(
            None
            if not rows
            else sum(not bool(row["is_current"]) for row in rows) / len(rows)
        ),
        mean_settlement_delay_seconds=(
            None if not delays else math.fsum(delays) / len(delays)
        ),
        brier_score=binary.brier_score,
        log_loss=binary.log_loss,
        calibration_drift_ece=binary.expected_calibration_error,
    )


def evaluate_prematch_prospective_gate(
    metrics: PrematchShadowMetrics,
    *,
    calibration_gate_passed: bool,
    incremental_gate_passed: bool,
) -> PrematchProspectiveDecision:
    reasons: list[str] = []
    if metrics.settled_support < PROSPECTIVE_MIN_SETTLED_MAPS:
        reasons.append("settled_prospective_maps_below_200")
    if metrics.formal_events < PROSPECTIVE_MIN_EVENTS:
        reasons.append("formal_events_below_5")
    if metrics.patches < PROSPECTIVE_MIN_PATCHES:
        reasons.append("patches_below_2")
    if (
        metrics.single_event_share is None
        or metrics.single_event_share > PROSPECTIVE_MAX_EVENT_SHARE
    ):
        reasons.append("single_event_share_above_0.40")
    if not calibration_gate_passed:
        reasons.append("prospective_calibration_gate_failed")
    if not incremental_gate_passed:
        reasons.append("prospective_incremental_gate_failed")
    if not calibration_gate_passed or not incremental_gate_passed:
        status = "unsupported"
    else:
        status = "passed" if not reasons else "shadow_collecting"
    return PrematchProspectiveDecision(status, tuple(reasons))


__all__ = [
    "PREMATCH_SHADOW_COMPONENTS",
    "PREMATCH_SHADOW_VERSION",
    "PrematchProspectiveDecision",
    "PrematchShadowCollection",
    "PrematchShadowMetrics",
    "PrematchShadowSettlement",
    "collect_prematch_shadow",
    "evaluate_prematch_prospective_gate",
    "load_prematch_shadow_metrics",
    "settle_ready_prematch_shadows",
]
