"""Prospective prematch shadow collection, settlement, and health metrics."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from database.session import PostgresSession
from live_betting.health import record_health

from .draft_features import AvailabilityMode
from .draft_model import evaluate_binary_predictions
from .prematch_artifacts import canonical_json_bytes
from .prematch_deployment import (
    FrozenPrematchDeployment,
    verify_frozen_prematch_deployment,
)
from .prematch_features import PrematchFeatureSnapshot, prematch_feature_schema
from .prematch_model import PrematchModelArtifact, fit_prematch_model, predict_prematch
from .prematch_report import (
    PREMATCH_BOOTSTRAP_SAMPLES,
    PairedMetricReport,
    _cluster_samples,
    _paired_metric,
    _PairedPoint,
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
from .team_rating_backtest import TeamRatingSourceAuthority, _source_availability


UTC = timezone.utc
PREMATCH_SHADOW_VERSION = "prematch-prospective-shadow-v1"
PREMATCH_SHADOW_COMPONENTS = (
    "prematch_team_rating",
    "prematch_draft_residual",
    "prematch_rosh",
    "prematch_clusters",
    "prematch_prediction",
    "prematch_settlement",
)
PROSPECTIVE_MIN_SETTLED_MAPS = 200
PROSPECTIVE_MIN_EVENTS = 5
PROSPECTIVE_MIN_PATCHES = 2
PROSPECTIVE_MAX_EVENT_SHARE = 0.40
ROLLING_METRIC_WINDOW = 200
CLUSTER_MODEL_KIND = "team_plus_draft_rosh_clusters"
NO_CLUSTER_MODEL_KIND = "team_plus_draft_rosh"


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
    mean_result_availability_delay_seconds: float | None
    mean_settlement_delay_seconds: float | None
    brier_score: float | None
    log_loss: float | None
    calibration_drift_ece: float | None
    paired_support: int
    paired_brier: PairedMetricReport
    paired_log_loss: PairedMetricReport
    incremental_gate_passed: bool
    cluster_paired_support: int
    cluster_candidate_brier_score: float | None
    no_cluster_candidate_brier_score: float | None
    cluster_paired_brier: PairedMetricReport
    cluster_paired_log_loss: PairedMetricReport
    cluster_incremental_gate_passed: bool
    cluster_status: str


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


def _freeze_shadow_context(
    connection: PostgresSession,
    prediction: PrematchPredictionRecord,
    model: PrematchModelArtifact,
    snapshot: PrematchFeatureSnapshot,
    cluster_candidate_model: PrematchModelArtifact | None = None,
) -> PrematchPredictionRecord:
    row = connection.execute(
        """SELECT COALESCE(ingest.series_id, match.series_id) AS series_id,
                  ingest.event_id, match.patch
             FROM matches AS match
             LEFT JOIN match_ingest_status AS ingest USING(match_id)
            WHERE match.match_id=?""",
        (prediction.match_id,),
    ).fetchone()
    if row is None:
        raise ValueError("prospective match metadata is unavailable")
    team_only_probability, team_only_model_hash = _team_only_comparison(
        model,
        snapshot,
        candidate_probability=prediction.raw_probability,
    )
    payload = json.loads(prediction.prediction_json)
    if model.model_kind == CLUSTER_MODEL_KIND:
        with_cluster_probability = prediction.raw_probability
        with_cluster_model_hash = model.model_hash
        without_cluster_probability, without_cluster_model_hash = _model_comparison(
            model,
            snapshot,
            model_kind=NO_CLUSTER_MODEL_KIND,
        )
        cluster_delta = prediction.cluster_logit_delta
        cluster_contributions = payload.get("top_cluster_contributions", [])
    elif cluster_candidate_model is not None:
        cluster_prediction = predict_prematch(cluster_candidate_model, snapshot)
        with_cluster_probability = cluster_prediction.raw_probability
        with_cluster_model_hash = cluster_candidate_model.model_hash
        without_cluster_probability = prediction.raw_probability
        without_cluster_model_hash = model.model_hash
        cluster_delta = cluster_prediction.cluster_logit_delta
        cluster_contributions = [
            row.to_payload()
            for row in getattr(cluster_prediction, "top_cluster_contributions", ())
        ]
    else:
        with_cluster_probability = None
        with_cluster_model_hash = None
        without_cluster_probability = prediction.raw_probability
        without_cluster_model_hash = model.model_hash
        cluster_delta = None
        cluster_contributions = []
    payload.update(
        {
            "candidate_probability": prediction.raw_probability,
            "team_only_probability": team_only_probability,
            "team_only_model_hash": team_only_model_hash,
            "candidate_without_cluster_probability": without_cluster_probability,
            "candidate_without_cluster_model_hash": without_cluster_model_hash,
            "candidate_with_cluster_probability": with_cluster_probability,
            "candidate_with_cluster_model_hash": with_cluster_model_hash,
            "cluster_candidate_logit_delta": cluster_delta,
            "top_cluster_contributions": cluster_contributions,
            "match_id": prediction.match_id,
            "series_id": row["series_id"],
            "event_id": (
                None if row["event_id"] is None else str(row["event_id"])
            ),
            "patch": None if row["patch"] is None else str(row["patch"]),
            "prediction_cutoff": prediction.prediction_cutoff.isoformat(),
        }
    )
    return replace(
        prediction,
        prediction_json=canonical_json_bytes(payload).decode("utf-8"),
    )


def _team_only_comparison(
    model: PrematchModelArtifact,
    snapshot: PrematchFeatureSnapshot,
    *,
    candidate_probability: float | None,
) -> tuple[float | None, str]:
    if model.model_kind == "team_only":
        return candidate_probability, model.model_hash
    return _model_comparison(model, snapshot, model_kind="team_only")


def _model_comparison(
    model: PrematchModelArtifact,
    snapshot: PrematchFeatureSnapshot,
    *,
    model_kind: str,
) -> tuple[float | None, str]:
    schema = prematch_feature_schema(model_kind)
    comparison = fit_prematch_model(
        (
            replace(
                source,
                features={
                    name: features[name]
                    for name in schema
                    if name in features
                },
            )
            for row in model.training_corpus
            for source in (row.to_training_row(),)
            for features in (dict(source.features),)
        ),
        model.training_cutoff,
        model_kind=model_kind,
        availability_mode=model.availability_mode,
        min_samples=model.min_samples,
        l2_regularization=model.l2_regularization,
    )
    return (
        predict_prematch(comparison, snapshot).raw_probability,
        comparison.model_hash,
    )


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
    cluster_candidate_model = (
        None
        if deployment.cluster_candidate_model_artifact is None
        else build_prematch_model_run_record(
            deployment.cluster_candidate_model_artifact
        )
    )
    calibration = build_prematch_calibration_record(
        deployment.calibration_artifact,
        model_hash=model.model_hash,
    )
    prediction = _freeze_shadow_context(
        connection,
        build_prematch_prediction_record(
            deployment.prematch_model_artifact,
            deployment.feature_snapshot,
            cutoff_source=cutoff_source,
            dependency_fingerprint=deployment.dependency_fingerprint,
            dependency_revision=dependency_revision,
            calibration=calibration,
        ),
        deployment.prematch_model_artifact,
        deployment.feature_snapshot,
        deployment.cluster_candidate_model_artifact,
    )
    validation = build_prematch_validation_record(
        model,
        prediction,
        validated_at=observed,
    )
    persistence = persist_prematch_records(
        connection,
        model_runs=(
            (model,)
            if cluster_candidate_model is None
            else (model, cluster_candidate_model)
        ),
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
        "prematch_clusters": (
            "healthy"
            if deployment.feature_snapshot.cluster_coverage > 0.0
            else "degraded",
            {
                "coverage": deployment.feature_snapshot.cluster_coverage,
                "support": deployment.feature_snapshot.cluster_support,
                "missing_reason": deployment.feature_snapshot.cluster_missing_reason,
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
                  match.radiant_win, match.start_time, match.duration,
                  source.artifact_id, source.content_hash,
                  source.first_usable_at AS artifact_usable_at,
                  (SELECT MIN(observation.first_usable_at)
                     FROM raw_source_observations AS observation
                    WHERE observation.artifact_id=source.artifact_id
                      AND observation.content_hash=source.content_hash
                      AND observation.first_usable_at IS NOT NULL
                  ) AS observation_usable_at
             FROM prematch_predictions AS prediction
             JOIN prematch_model_runs AS run USING(run_id)
             JOIN matches AS match USING(match_id)
             JOIN match_ingest_status AS ingest USING(match_id)
             JOIN raw_source_artifacts AS source
               ON source.artifact_id=ingest.latest_raw_artifact_id
              AND source.content_hash=ingest.latest_raw_content_hash
              AND source.source='opendota'
            WHERE run.availability_mode='prospective'
              AND prediction.status='predicted'
              AND match.radiant_win IS NOT NULL
            ORDER BY prediction.match_id, prediction.run_id""",
    ).fetchall()
    eligible = 0
    updated = 0
    unchanged = 0
    for row in rows:
        completed_at = datetime.fromtimestamp(int(row["start_time"]), UTC) + timedelta(
            seconds=int(row["duration"])
        )
        authority = TeamRatingSourceAuthority(
            match_id=int(row["match_id"]),
            artifact_id=str(row["artifact_id"]),
            content_hash=str(row["content_hash"]),
            artifact_usable_at=(
                None
                if row["artifact_usable_at"] is None
                else _parse_utc(row["artifact_usable_at"], "artifact first_usable_at")
            ),
            observation_usable_at=(
                None
                if row["observation_usable_at"] is None
                else _parse_utc(
                    row["observation_usable_at"],
                    "observation first_usable_at",
                )
            ),
        )
        result_usable_at, _source = _source_availability(authority)
        if result_usable_at is None or result_usable_at > observed:
            continue
        if result_usable_at < completed_at:
            raise ValueError("authoritative result precedes match completion")
        eligible += 1
        result = settle_prematch_prediction(
            connection,
            run_id=str(row["run_id"]),
            match_id=int(row["match_id"]),
            eventual_radiant_win=bool(row["radiant_win"]),
            result_usable_at=result_usable_at,
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
            "eligible": eligible,
            "updated": updated,
            "unchanged": unchanged,
        },
    )
    return PrematchShadowSettlement(eligible, updated, unchanged)


def load_prematch_shadow_metrics(
    connection: PostgresSession,
) -> PrematchShadowMetrics:
    rows = connection.execute(
        """SELECT prediction.match_id, prediction.team_base_probability,
                  prediction.raw_probability,
                  prediction.calibrated_probability,
                  prediction.coverage, prediction.rosh_logit_delta,
                  prediction.prediction_json,
                  prediction.eventual_radiant_win,
                  prediction.result_usable_at, prediction.settled_at,
                  prediction.status, ingest.event_id,
                  COALESCE(ingest.series_id, match.series_id) AS series_id,
                  match.patch, match.start_time, match.duration,
                  prematch_lineage_revision_is_current(
                      validation.dependency_revision,
                      prediction.prediction_cutoff
                  ) AS is_current
             FROM prematch_predictions AS prediction
             JOIN prematch_model_runs AS run USING(run_id)
             JOIN prematch_prediction_validations AS validation
               ON validation.run_id=prediction.run_id
              AND validation.match_id=prediction.match_id
             LEFT JOIN match_ingest_status AS ingest
               ON ingest.match_id=prediction.match_id
             LEFT JOIN matches AS match
               ON match.match_id=prediction.match_id
            WHERE run.availability_mode='prospective'
            ORDER BY prediction.prediction_cutoff DESC,
                     prediction.match_id DESC, prediction.run_id"""
    ).fetchall()
    prediction_support = len(rows)
    settled = [row for row in rows if str(row["status"]) == "settled"]
    metric_rows = settled[:ROLLING_METRIC_WINDOW]
    probabilities: list[float] = []
    outcomes: list[bool] = []
    paired: list[_PairedPoint] = []
    cluster_paired: list[_PairedPoint] = []
    for row in metric_rows:
        payload = json.loads(str(row["prediction_json"]))
        candidate_probability = payload.get(
            "candidate_probability",
            row["raw_probability"],
        )
        team_only = payload.get("team_only_probability")
        team_only_model_hash = payload.get("team_only_model_hash")
        outcome = row["eventual_radiant_win"]
        if outcome is None:
            continue
        monitored_probability = row["calibrated_probability"]
        if monitored_probability is None:
            monitored_probability = candidate_probability
        if monitored_probability is not None:
            probabilities.append(float(monitored_probability))
            outcomes.append(bool(outcome))
        if (
            candidate_probability is None
            or team_only is None
            or not isinstance(team_only_model_hash, str)
        ):
            continue
        series_id = (
            payload["series_id"] if "series_id" in payload else row["series_id"]
        )
        paired.append(
            _PairedPoint(
                match_id=int(row["match_id"]),
                series_id=None if series_id is None else int(series_id),
                outcome=bool(outcome),
                candidate_probability=float(candidate_probability),
                baseline_probability=float(team_only),
            )
        )
        with_cluster = payload.get("candidate_with_cluster_probability")
        without_cluster = payload.get("candidate_without_cluster_probability")
        if (
            with_cluster is not None
            and without_cluster is not None
            and isinstance(payload.get("candidate_with_cluster_model_hash"), str)
            and isinstance(payload.get("candidate_without_cluster_model_hash"), str)
            and isinstance(payload.get("cluster_feature_snapshot_hash"), str)
            and isinstance(payload.get("cluster_resource_hash"), str)
        ):
            cluster_paired.append(
                _PairedPoint(
                    match_id=int(row["match_id"]),
                    series_id=None if series_id is None else int(series_id),
                    outcome=bool(outcome),
                    candidate_probability=float(with_cluster),
                    baseline_probability=float(without_cluster),
                )
            )
    binary = evaluate_binary_predictions(outcomes, probabilities, ece_bins=5)
    samples = _cluster_samples(
        paired,
        series_id=lambda row: row.series_id,
        match_id=lambda row: row.match_id,
        seed_material=f"{PREMATCH_SHADOW_VERSION}:candidate-vs-team-only",
        samples=PREMATCH_BOOTSTRAP_SAMPLES,
    )
    paired_brier = _paired_metric(paired, samples, "brier_score")
    paired_log_loss = _paired_metric(paired, samples, "log_loss")
    incremental_gate_passed = len(paired) >= PROSPECTIVE_MIN_SETTLED_MAPS and all(
        metric.delta is not None
        and metric.delta < 0.0
        and metric.ci_90.upper is not None
        and metric.ci_90.upper < 0.0
        for metric in (paired_brier, paired_log_loss)
    )
    cluster_samples = _cluster_samples(
        cluster_paired,
        series_id=lambda row: row.series_id,
        match_id=lambda row: row.match_id,
        seed_material=f"{PREMATCH_SHADOW_VERSION}:cluster-vs-no-cluster",
        samples=PREMATCH_BOOTSTRAP_SAMPLES,
    )
    cluster_paired_brier = _paired_metric(
        cluster_paired,
        cluster_samples,
        "brier_score",
    )
    cluster_paired_log_loss = _paired_metric(
        cluster_paired,
        cluster_samples,
        "log_loss",
    )
    cluster_brier_passed = (
        cluster_paired_brier.delta is not None
        and cluster_paired_brier.delta < 0.0
        and cluster_paired_brier.ci_90.upper is not None
        and cluster_paired_brier.ci_90.upper < 0.0
    )
    cluster_log_loss_clearly_worse = (
        cluster_paired_log_loss.delta is not None
        and cluster_paired_log_loss.delta > 0.0
        and cluster_paired_log_loss.ci_90.lower is not None
        and cluster_paired_log_loss.ci_90.lower > 0.0
    )
    cluster_incremental_gate_passed = (
        len(cluster_paired) >= PROSPECTIVE_MIN_SETTLED_MAPS
        and cluster_brier_passed
        and not cluster_log_loss_clearly_worse
    )
    cluster_status = (
        "collecting"
        if len(cluster_paired) < PROSPECTIVE_MIN_SETTLED_MAPS
        else "passed"
        if cluster_incremental_gate_passed
        else "failed"
    )

    def mean_brier(points: list[_PairedPoint], *, candidate: bool) -> float | None:
        if not points:
            return None
        return math.fsum(
            (
                (row.candidate_probability if candidate else row.baseline_probability)
                - float(row.outcome)
            )
            ** 2
            for row in points
        ) / len(points)
    events = Counter(
        str(
            payload["event_id"]
            if "event_id" in payload
            else row["event_id"]
        )
        for row in settled
        for payload in (json.loads(str(row["prediction_json"])),)
        if (
            payload["event_id"] if "event_id" in payload else row["event_id"]
        )
        is not None
        and str(
            payload["event_id"]
            if "event_id" in payload
            else row["event_id"]
        ).strip()
    )
    patches = {
        str(payload["patch"] if "patch" in payload else row["patch"])
        for row in settled
        for payload in (json.loads(str(row["prediction_json"])),)
        if (payload["patch"] if "patch" in payload else row["patch"])
        is not None
        and str(payload["patch"] if "patch" in payload else row["patch"]).strip()
    }
    result_availability_delays = []
    delays = [
        (
            _parse_utc(row["settled_at"], "settled_at")
            - _parse_utc(row["result_usable_at"], "result_usable_at")
        ).total_seconds()
        for row in settled
    ]
    for row in settled:
        completed_at = datetime.fromtimestamp(int(row["start_time"]), UTC) + timedelta(
            seconds=int(row["duration"])
        )
        result_usable_at = _parse_utc(row["result_usable_at"], "result_usable_at")
        if result_usable_at < completed_at:
            raise ValueError("stored result authority precedes match completion")
        result_availability_delays.append(
            (result_usable_at - completed_at).total_seconds()
        )
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
        mean_result_availability_delay_seconds=(
            None
            if not result_availability_delays
            else math.fsum(result_availability_delays)
            / len(result_availability_delays)
        ),
        mean_settlement_delay_seconds=(
            None if not delays else math.fsum(delays) / len(delays)
        ),
        brier_score=binary.brier_score,
        log_loss=binary.log_loss,
        calibration_drift_ece=binary.expected_calibration_error,
        paired_support=len(paired),
        paired_brier=paired_brier,
        paired_log_loss=paired_log_loss,
        incremental_gate_passed=incremental_gate_passed,
        cluster_paired_support=len(cluster_paired),
        cluster_candidate_brier_score=mean_brier(
            cluster_paired,
            candidate=True,
        ),
        no_cluster_candidate_brier_score=mean_brier(
            cluster_paired,
            candidate=False,
        ),
        cluster_paired_brier=cluster_paired_brier,
        cluster_paired_log_loss=cluster_paired_log_loss,
        cluster_incremental_gate_passed=cluster_incremental_gate_passed,
        cluster_status=cluster_status,
    )


def evaluate_prematch_prospective_gate(
    metrics: PrematchShadowMetrics,
    *,
    calibration_gate_passed: bool,
) -> PrematchProspectiveDecision:
    reasons: list[str] = []
    if metrics.settled_support < PROSPECTIVE_MIN_SETTLED_MAPS:
        reasons.append("settled_prospective_maps_below_200")
    if metrics.paired_support < PROSPECTIVE_MIN_SETTLED_MAPS:
        reasons.append("paired_prospective_maps_below_200")
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
    if (
        metrics.paired_support >= PROSPECTIVE_MIN_SETTLED_MAPS
        and not metrics.incremental_gate_passed
    ):
        reasons.append("prospective_incremental_gate_failed")
    collecting = (
        metrics.settled_support < PROSPECTIVE_MIN_SETTLED_MAPS
        or metrics.paired_support < PROSPECTIVE_MIN_SETTLED_MAPS
        or metrics.formal_events < PROSPECTIVE_MIN_EVENTS
        or metrics.patches < PROSPECTIVE_MIN_PATCHES
        or metrics.single_event_share is None
        or metrics.single_event_share > PROSPECTIVE_MAX_EVENT_SHARE
    )
    if collecting:
        status = "collecting"
    elif not calibration_gate_passed or not metrics.incremental_gate_passed:
        status = "failed"
    else:
        status = "passed"
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
