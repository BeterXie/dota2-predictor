"""Build frozen pure-draft artifacts without blocking the live strategy loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from database.session import PostgresSession

from .backtest import (
    BACKTEST_VERSION,
    DraftDependencyLimitError,
    HORIZONS,
    _draft_snapshot_rows,
    draft_dependency_fingerprint,
    load_bounded_draft_snapshot,
    load_draft_corpus,
)
from .draft_artifacts import (
    CalibrationSample,
    DraftCalibrationArtifact,
    assert_model_artifact_deployable,
    build_calibration_artifact,
    canonical_hash,
    canonical_json_bytes,
)
from .draft_features import AvailabilityMode, DraftMapEvidence
from .draft_model import (
    DEFAULT_L2_REGULARIZATION,
    DEFAULT_MIN_SAMPLES,
    DraftModelArtifact,
    DraftTrainingRow,
    FeatureSchema,
    ModelStatus,
    fit_draft_model,
)
from .incremental import current_derived_scopes
from .roles import (
    PROSPECTIVE_ASSIGNMENT_VERSION,
    RECONSTRUCTED_ASSIGNMENT_VERSION,
)


UTC = timezone.utc
DEPLOYMENT_VERSION = "frozen-pure-draft-deployment-v2"
MIN_CALIBRATION_FIT_SUPPORT = 20
MIN_CALIBRATION_EVALUATION_SUPPORT = 100


@dataclass(frozen=True)
class FrozenDraftDeployment:
    deployment_key: str
    training_cutoff: datetime
    dependency_fingerprint: str
    dependency_revision: int
    models: tuple[DraftModelArtifact, ...]
    calibrations: tuple[DraftCalibrationArtifact, ...]

    def model(self, horizon_minutes: int) -> DraftModelArtifact:
        return next(row for row in self.models if row.horizon_minutes == horizon_minutes)

    def calibration(self, horizon_minutes: int) -> DraftCalibrationArtifact:
        return next(
            row for row in self.calibrations if row.horizon_minutes == horizon_minutes
        )

    @property
    def evidence_mode(self) -> str:
        modes = {row.evidence_mode for row in self.calibrations}
        if len(modes) != 1:
            raise ValueError("deployment calibration evidence modes disagree")
        return next(iter(modes))

    def to_identity_payload(self) -> dict[str, object]:
        return {
            "deployment_version": DEPLOYMENT_VERSION,
            "training_cutoff": self.training_cutoff.isoformat(),
            "dependency_fingerprint": self.dependency_fingerprint,
            "dependency_revision": self.dependency_revision,
            "model_hashes": {
                str(row.horizon_minutes): row.model_hash for row in self.models
            },
            "calibration_hashes": {
                str(row.horizon_minutes): row.calibration_hash
                for row in self.calibrations
            },
            "evidence_mode": self.evidence_mode,
        }


def _parse_utc(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _actual_result_availability(
    connection: PostgresSession,
) -> dict[int, datetime]:
    rows = connection.execute(
        """SELECT status.match_id, artifact.first_usable_at,
                  MIN(observation.first_usable_at)
             FROM match_ingest_status AS status
             JOIN raw_source_artifacts AS artifact
               ON artifact.artifact_id=status.latest_raw_artifact_id
              AND artifact.content_hash=status.latest_raw_content_hash
             JOIN raw_source_observations AS observation
               ON observation.artifact_id=artifact.artifact_id
              AND observation.content_hash=artifact.content_hash
            WHERE artifact.first_usable_at IS NOT NULL
              AND observation.first_usable_at IS NOT NULL
            GROUP BY status.match_id, artifact.first_usable_at"""
    ).fetchall()
    result: dict[int, datetime] = {}
    for row in rows:
        artifact_at = _parse_utc(row[1])
        observation_at = _parse_utc(row[2])
        if artifact_at is not None and observation_at is not None:
            result[int(row[0])] = max(artifact_at, observation_at)
    return result


def _training_rows(
    connection: PostgresSession,
    training_cutoff: datetime,
) -> tuple[tuple[DraftTrainingRow, ...], tuple[str, ...]]:
    corpus = load_draft_corpus(
        connection,
        availability_mode=AvailabilityMode.RECONSTRUCTED,
        assignment_version=RECONSTRUCTED_ASSIGNMENT_VERSION,
    )
    snapshots = _draft_snapshot_rows(corpus)
    result_available = _actual_result_availability(connection)
    schema_names: tuple[str, ...] | None = None
    rows: list[DraftTrainingRow] = []
    for row in snapshots:
        target = row.game.target
        available_at = result_available.get(row.game.match_id)
        if (
            target is None
            or target.prediction_cutoff >= training_cutoff
            or available_at is None
            or available_at > training_cutoff
        ):
            continue
        features = row.snapshot.pure_values()
        names = tuple(sorted(features))
        if schema_names is None:
            schema_names = names
        elif schema_names != names:
            raise ValueError("pure-draft feature schema changed within the corpus")
        rows.append(
            DraftTrainingRow(
                match_id=row.game.match_id,
                input_snapshot_hash=row.snapshot.input_hash,
                cutoff=target.prediction_cutoff,
                completed_at=row.game.evidence.completed_at,
                result_usable_at=available_at,
                outcome=row.game.radiant_win,
                duration_minutes=row.game.duration_seconds / 60.0,
                series_id=(
                    row.game.series_id
                    if row.game.series_id is not None
                    else f"match:{row.game.match_id}"
                ),
                features=features,
            )
        )
    if schema_names is None:
        raise ValueError("no causally available draft training snapshots")
    return tuple(rows), schema_names


def assert_draft_models_match_database(
    connection: PostgresSession,
    models: Iterable[DraftModelArtifact],
    *,
    training_cutoff: datetime,
) -> None:
    """Rebuild deployment models from the authoritative cutoff corpus."""

    if training_cutoff.tzinfo is None or training_cutoff.utcoffset() is None:
        raise ValueError("training_cutoff must be timezone-aware")
    cutoff = training_cutoff.astimezone(UTC)
    ordered = tuple(sorted(models, key=lambda row: row.horizon_minutes))
    if tuple(row.horizon_minutes for row in ordered) != tuple(HORIZONS):
        raise ValueError("database model replay requires all five horizons")

    def verify() -> None:
        training_rows, feature_names = _training_rows(connection, cutoff)
        schema = FeatureSchema.from_names(feature_names)
        for model in ordered:
            assert_model_artifact_deployable(model)
            if model.training_cutoff != cutoff:
                raise ValueError("deployment model training cutoffs disagree")
            rebuilt = fit_draft_model(
                training_rows,
                schema,
                cutoff,
                model.horizon_minutes,
                min_samples=model.min_samples,
                model_kind=model.model_kind,
                l2_regularization=model.l2_regularization,
            )
            if canonical_json_bytes(rebuilt.to_payload()) != canonical_json_bytes(
                model.to_payload()
            ):
                raise ValueError(
                    "model artifact does not match the authoritative database corpus"
                )
    if connection.in_transaction:
        verify()
    else:
        with connection.transaction():
            verify()


def _current_calibration_samples(
    connection: PostgresSession,
    training_cutoff: datetime,
) -> dict[int, tuple[CalibrationSample, ...]]:
    scopes = current_derived_scopes(connection)
    current = set(scopes.draft_predictions)
    grouped: dict[int, list[CalibrationSample]] = {horizon: [] for horizon in HORIZONS}
    rows = connection.execute(
        """SELECT run.run_id, run.horizon_minutes, prediction.match_id,
                  prediction.probability, prediction.eventual_radiant_win,
                  prediction.prediction_cutoff, status.series_id,
                  status.event_id,
                  GREATEST(
                      artifact.first_usable_at,
                      (SELECT MIN(observation.first_usable_at)
                         FROM raw_source_observations AS observation
                        WHERE observation.artifact_id=artifact.artifact_id
                          AND observation.content_hash=artifact.content_hash
                          AND observation.first_usable_at IS NOT NULL)
                  ) AS result_usable_at
             FROM draft_predictions AS prediction
             JOIN draft_model_runs AS run ON run.run_id=prediction.run_id
             JOIN match_ingest_status AS status
               ON status.match_id=prediction.match_id
             JOIN raw_source_artifacts AS artifact
               ON artifact.artifact_id=status.latest_raw_artifact_id
              AND artifact.content_hash=status.latest_raw_content_hash
            WHERE run.model_kind='pure_draft'
              AND run.availability_mode='reconstructed_walk_forward'
              AND prediction.status='settled'
              AND prediction.probability IS NOT NULL
              AND prediction.eventual_radiant_win IN (0, 1)
              AND artifact.first_usable_at IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM raw_source_observations AS observation
                   WHERE observation.artifact_id=artifact.artifact_id
                     AND observation.content_hash=artifact.content_hash
                     AND observation.first_usable_at IS NOT NULL
              )
            ORDER BY prediction.prediction_cutoff, run.run_id,
                     prediction.match_id"""
    ).fetchall()
    for row in rows:
        key = (str(row[0]), int(row[2]))
        if key not in current:
            continue
        observed_at = _parse_utc(row[5])
        settled_at = _parse_utc(row[8])
        if (
            observed_at is None
            or settled_at is None
            or settled_at > training_cutoff
            or settled_at < observed_at
        ):
            continue
        horizon = int(row[1])
        if horizon not in grouped:
            continue
        grouped[horizon].append(
            CalibrationSample(
                sample_id=f"{row[0]}:{int(row[2])}",
                probability=float(row[3]),
                outcome=int(row[4]),
                observed_at=observed_at,
                settled_at=settled_at,
                cluster_id=(
                    f"series:{int(row[6])}"
                    if row[6] is not None
                    else f"match:{int(row[2])}"
                ),
                event_id=str(row[7]),
            )
        )
    return {
        horizon: tuple(
            sorted(values, key=lambda value: (value.observed_at, value.sample_id))
        )
        for horizon, values in grouped.items()
    }


def split_calibration_samples(
    samples: Iterable[CalibrationSample],
) -> tuple[tuple[CalibrationSample, ...], tuple[CalibrationSample, ...]]:
    ordered = tuple(sorted(samples, key=lambda row: (row.observed_at, row.sample_id)))
    best: tuple[tuple[CalibrationSample, ...], tuple[CalibrationSample, ...]] | None = None
    for index in range(MIN_CALIBRATION_FIT_SUPPORT, len(ordered)):
        fit = ordered[:index]
        evaluation = ordered[index:]
        if len(evaluation) < MIN_CALIBRATION_EVALUATION_SUPPORT:
            break
        if max(row.settled_at for row in fit) <= min(
            row.observed_at for row in evaluation
        ):
            best = fit, evaluation
    if best is not None:
        return best
    # An identity calibrator can still be evaluated without borrowing a future
    # result into its fit. It remains research-only unless prospective evidence
    # independently passes every gate.
    return (), ordered


def build_frozen_draft_deployment(
    connection: PostgresSession,
    *,
    training_cutoff: datetime,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    l2_regularization: float = DEFAULT_L2_REGULARIZATION,
) -> FrozenDraftDeployment:
    """Build five immutable models and causally separated calibration artifacts."""

    if training_cutoff.tzinfo is None or training_cutoff.utcoffset() is None:
        raise ValueError("training_cutoff must be timezone-aware")
    cutoff = training_cutoff.astimezone(UTC)
    revision = connection.execute(
        """SELECT dependency_revision FROM draft_lineage_revisions
             WHERE singleton=1"""
    ).fetchone()
    if revision is None:
        raise ValueError("draft dependency revision is unavailable")
    dependency_revision = int(revision[0])
    dependency_fingerprint = draft_dependency_fingerprint(connection)
    training_rows, feature_names = _training_rows(connection, cutoff)
    schema = FeatureSchema.from_names(feature_names)
    samples = _current_calibration_samples(connection, cutoff)
    models: list[DraftModelArtifact] = []
    calibrations: list[DraftCalibrationArtifact] = []
    for horizon in HORIZONS:
        model = fit_draft_model(
            training_rows,
            schema,
            cutoff,
            horizon,
            min_samples=min_samples,
            model_kind="pure_draft",
            l2_regularization=l2_regularization,
        )
        if model.status is not ModelStatus.TRAINED:
            raise ValueError(
                f"horizon {horizon} deployment model is not trained: {model.reason}"
            )
        fit, evaluation = split_calibration_samples(samples[horizon])
        calibration = build_calibration_artifact(
            model,
            evidence_mode="reconstructed_walk_forward",
            source_ref=(
                f"{BACKTEST_VERSION}:current-lineage:{dependency_fingerprint}"
            ),
            fit_samples=fit,
            evaluation_samples=evaluation,
        )
        models.append(model)
        calibrations.append(calibration)
    identity = {
        "deployment_version": DEPLOYMENT_VERSION,
        "training_cutoff": cutoff.isoformat(),
        "dependency_fingerprint": dependency_fingerprint,
        "dependency_revision": dependency_revision,
        "model_hashes": {
            str(row.horizon_minutes): row.model_hash for row in models
        },
        "calibration_hashes": {
            str(row.horizon_minutes): row.calibration_hash
            for row in calibrations
        },
        "evidence_mode": "reconstructed_walk_forward",
    }
    return FrozenDraftDeployment(
        deployment_key=canonical_hash(identity),
        training_cutoff=cutoff,
        dependency_fingerprint=dependency_fingerprint,
        dependency_revision=dependency_revision,
        models=tuple(models),
        calibrations=tuple(calibrations),
    )


def load_prospective_history(
    connection: PostgresSession,
) -> tuple[DraftMapEvidence, ...]:
    """Load only historical facts whose real archive availability is known."""

    available = connection.execute(
        """SELECT 1
             FROM player_role_assignments AS roles
             JOIN formal_map_eligibility AS eligible
               ON eligible.match_id=roles.match_id
            WHERE eligible.draft_readiness='ready'
              AND roles.purpose='expected_position'
              AND roles.assignment_version=?
            LIMIT 1""",
        (PROSPECTIVE_ASSIGNMENT_VERSION,),
    ).fetchone()
    if available is None:
        return ()
    corpus = load_draft_corpus(
        connection,
        availability_mode=AvailabilityMode.PROSPECTIVE,
        assignment_version=PROSPECTIVE_ASSIGNMENT_VERSION,
    )
    return tuple(row.evidence for row in corpus.maps)


def load_bounded_prospective_history(
    connection: PostgresSession,
    *,
    max_rows: int,
    max_bytes: int,
    max_value_bytes: int,
) -> tuple[str, tuple[DraftMapEvidence, ...]]:
    """Load runtime history only after bounded dependency SQL preflight."""

    available = connection.execute(
        """SELECT 1
             FROM player_role_assignments AS roles
             JOIN formal_map_eligibility AS eligible
               ON eligible.match_id=roles.match_id
            WHERE eligible.draft_readiness='ready'
              AND roles.purpose='expected_position'
              AND roles.assignment_version=?
            LIMIT 1""",
        (PROSPECTIVE_ASSIGNMENT_VERSION,),
    ).fetchone()
    fingerprint, corpus = load_bounded_draft_snapshot(
        connection,
        availability_mode=AvailabilityMode.PROSPECTIVE,
        assignment_version=PROSPECTIVE_ASSIGNMENT_VERSION,
        max_rows=max_rows,
        max_bytes=max_bytes,
        max_value_bytes=max_value_bytes,
    )
    if available is None:
        return fingerprint, ()
    return fingerprint, tuple(row.evidence for row in corpus.maps)


def deployment_summary(deployment: FrozenDraftDeployment) -> str:
    return json.dumps(deployment.to_identity_payload(), sort_keys=True)


__all__ = [
    "DEPLOYMENT_VERSION",
    "FrozenDraftDeployment",
    "assert_draft_models_match_database",
    "build_frozen_draft_deployment",
    "deployment_summary",
    "DraftDependencyLimitError",
    "load_bounded_prospective_history",
    "load_prospective_history",
    "split_calibration_samples",
]
