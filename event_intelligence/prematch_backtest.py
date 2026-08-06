"""Causal M6 walk-forward evaluation for the prematch model family."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping

from database.session import PostgresSession
from live_betting.rosh_parity_storage import (
    RoshRunMatchLink,
    RoshRunRepository,
    StoredRoshRun,
)

from .backtest import DraftCorpus, LoadedDraftMap, load_draft_corpus
from .cluster_artifacts import ClusterFeatureArtifact, build_cluster_feature_artifact
from .cluster_features import ClusterFeatureTarget, ClusterPlayer
from .draft_features import (
    ROLE_CONFIDENCE_MIN,
    AvailabilityMode,
    DraftTarget,
    DraftTeam,
)
from .draft_residual_features import (
    TeamRatingResidualEvidenceCache,
    build_draft_residual_snapshot_with_authority,
    build_team_rating_residual_evidence_cache,
)
from .hero_clusters import ClusterEvidenceMode, ClusterResource, load_cluster_resource
from .prematch_calibration import (
    PrematchCalibrationArtifact,
    PrematchCalibrationSample,
    build_prematch_calibration_artifact,
)
from .prematch_features import (
    PREMATCH_MODEL_KINDS,
    PrematchFeatureSnapshot,
    build_prematch_feature_snapshot,
    verify_prematch_feature_snapshot,
)
from .prematch_model import (
    DEFAULT_L2_REGULARIZATION,
    DEFAULT_MIN_SAMPLES,
    PredictionStatus,
    PrematchModelArtifact,
    PrematchPrediction,
    PrematchTrainingRow,
    fit_prematch_model,
    predict_prematch,
)
from .rosh_features import (
    RoshFeatureSnapshot,
    RoshFeatureTarget,
    RoshRequestPlanWitness,
    build_rosh_feature_snapshot_with_authority,
    build_unavailable_rosh_feature_snapshot_with_authority,
)
from .rosh_authority_bridge import load_rosh_bridge_witnesses
from .roles import PROSPECTIVE_ASSIGNMENT_VERSION, RECONSTRUCTED_ASSIGNMENT_VERSION
from .storage import IntelligenceStorage
from .team_rating import RatingMapInput
from .team_rating_backtest import (
    TeamRatingCorpus,
    TeamRatingWalkForwardRun,
    build_team_rating_walk_forward_runs,
    load_team_rating_corpus,
)

if TYPE_CHECKING:
    from .prematch_report import PrematchBacktestReport


UTC = timezone.utc
PREMATCH_BACKTEST_VERSION = "prematch-walk-forward-v1"
CLUSTER_MODEL_KIND = "team_plus_draft_rosh_clusters"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _utc(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value


def _probability(value: object, field: str, *, strict: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a probability")
    result = float(value)
    valid = 0.0 < result < 1.0 if strict else 0.0 <= result <= 1.0
    if not math.isfinite(result) or not valid:
        raise ValueError(f"{field} must be a probability")
    return result


def _series_key(series_id: int | None, match_id: int) -> str:
    return f"series:{series_id}" if series_id is not None else f"match:{match_id}"


@dataclass(frozen=True)
class PrematchBacktestTarget:
    """One formal target and the externally replayed M5 snapshot, if available."""

    match_id: int
    series_id: int | None
    event_id: str
    patch_id: str | None
    prediction_cutoff: datetime
    completed_at: datetime
    result_usable_at: datetime | None
    cutoff_source: str
    availability_mode: str
    outcome: bool
    team_base_probability: float
    radiant_prior_probability: float
    snapshot: PrematchFeatureSnapshot | None
    failure_reason: str | None

    def __post_init__(self) -> None:
        match_id = _positive_int(self.match_id, "match_id")
        if self.series_id is not None:
            _positive_int(self.series_id, "series_id")
        object.__setattr__(self, "event_id", _nonempty(self.event_id, "event_id"))
        if self.patch_id is not None:
            object.__setattr__(
                self,
                "patch_id",
                _nonempty(self.patch_id, "patch_id"),
            )
        cutoff = _utc(self.prediction_cutoff, "prediction_cutoff")
        completed = _utc(self.completed_at, "completed_at")
        usable = (
            None
            if self.result_usable_at is None
            else _utc(self.result_usable_at, "result_usable_at")
        )
        if completed <= cutoff:
            raise ValueError("target completion must follow prediction cutoff")
        if usable is not None and usable < completed:
            raise ValueError("target result cannot be usable before completion")
        object.__setattr__(self, "prediction_cutoff", cutoff)
        object.__setattr__(self, "completed_at", completed)
        object.__setattr__(self, "result_usable_at", usable)
        object.__setattr__(
            self,
            "cutoff_source",
            _nonempty(self.cutoff_source, "cutoff_source"),
        )
        mode = AvailabilityMode(self.availability_mode).value
        object.__setattr__(self, "availability_mode", mode)
        if not isinstance(self.outcome, bool):
            raise ValueError("outcome must be boolean")
        team_probability = _probability(
            self.team_base_probability,
            "team_base_probability",
            strict=True,
        )
        object.__setattr__(self, "team_base_probability", team_probability)
        object.__setattr__(
            self,
            "radiant_prior_probability",
            _probability(
                self.radiant_prior_probability,
                "radiant_prior_probability",
                strict=True,
            ),
        )
        if self.snapshot is None:
            if self.failure_reason is None:
                raise ValueError("an unavailable target requires a failure reason")
            _nonempty(self.failure_reason, "failure_reason")
            return

        if self.failure_reason is not None or self.patch_id is None:
            raise ValueError("an available target cannot carry a failure reason")
        verify_prematch_feature_snapshot(self.snapshot)
        if (
            self.snapshot.match_id != match_id
            or self.snapshot.prediction_cutoff != cutoff
            or self.snapshot.availability_mode != mode
        ):
            raise ValueError("target and prematch snapshot identities disagree")
        expected_logit = math.log(team_probability) - math.log1p(-team_probability)
        if not math.isclose(
            self.snapshot.team_base_logit,
            expected_logit,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("target and prematch Team Rating offsets disagree")


@dataclass(frozen=True)
class PrematchCorpus:
    availability_mode: str
    formal_maps: int
    targets: tuple[PrematchBacktestTarget, ...]
    team_rating_runs: tuple[TeamRatingWalkForwardRun, ...] = ()
    draft_corpus: DraftCorpus | None = None
    rosh_runs: tuple[StoredRoshRun, ...] = ()
    rosh_match_links: tuple[RoshRunMatchLink, ...] = ()
    dependency_revision: int | None = None

    def __post_init__(self) -> None:
        mode = AvailabilityMode(self.availability_mode).value
        object.__setattr__(self, "availability_mode", mode)
        _nonnegative_int(self.formal_maps, "formal_maps")
        if len(self.targets) > self.formal_maps:
            raise ValueError("prematch targets cannot exceed formal maps")
        ordered = tuple(
            sorted(self.targets, key=lambda row: (row.prediction_cutoff, row.match_id))
        )
        if ordered != self.targets:
            raise ValueError("prematch targets must be chronologically ordered")
        match_ids = tuple(row.match_id for row in self.targets)
        if len(match_ids) != len(set(match_ids)):
            raise ValueError("prematch target match IDs must be unique")
        if any(row.availability_mode != mode for row in self.targets):
            raise ValueError("prematch corpus cannot mix availability modes")
        if self.dependency_revision is not None:
            _positive_int(self.dependency_revision, "dependency_revision")

    @property
    def eligible_targets(self) -> int:
        return len(self.targets)


@dataclass(frozen=True)
class PrematchWalkForwardRun:
    target: PrematchBacktestTarget
    model_kind: str
    model_artifact: PrematchModelArtifact
    prediction: PrematchPrediction
    calibration_sample: PrematchCalibrationSample | None

    def __post_init__(self) -> None:
        if self.target.snapshot is None:
            raise ValueError("walk-forward runs require an available snapshot")
        if self.model_kind not in PREMATCH_MODEL_KINDS:
            raise ValueError("unsupported prematch model kind")
        model = self.model_artifact
        if (
            model.model_kind != self.model_kind
            or model.availability_mode != self.target.availability_mode
            or model.training_cutoff != self.target.prediction_cutoff
        ):
            raise ValueError("walk-forward model identity does not match target")
        if self.prediction.model_hash != model.model_hash:
            raise ValueError("walk-forward prediction model hash does not match")
        if self.prediction.input_snapshot_hash != self.target.snapshot.input_hash:
            raise ValueError("walk-forward prediction snapshot hash does not match")
        if any(row.match_id == self.target.match_id for row in model.training_corpus):
            raise ValueError("target match entered its own model training corpus")
        predicted = self.prediction.status is PredictionStatus.PREDICTED
        should_sample = predicted and self.target.result_usable_at is not None
        if should_sample != (self.calibration_sample is not None):
            raise ValueError("walk-forward calibration sample is inconsistent")
        if self.calibration_sample is not None:
            sample = self.calibration_sample
            if (
                sample.match_id != self.target.match_id
                or sample.model_kind != self.model_kind
                or sample.model_hash != model.model_hash
                or sample.input_snapshot_hash != self.target.snapshot.input_hash
            ):
                raise ValueError("walk-forward calibration sample identity disagrees")


@dataclass(frozen=True)
class PrematchFinalModel:
    model_artifact: PrematchModelArtifact
    calibration_artifact: PrematchCalibrationArtifact

    def __post_init__(self) -> None:
        model = self.model_artifact
        calibration = self.calibration_artifact
        if (
            model.model_kind != calibration.model_kind
            or model.availability_mode != calibration.availability_mode
            or model.training_cutoff != calibration.calibration_cutoff
        ):
            raise ValueError("final model and calibration identities disagree")


@dataclass(frozen=True)
class PrematchBacktestResult:
    backtest_version: str
    availability_mode: str
    evaluation_cutoff: datetime
    corpus: PrematchCorpus
    walk_forward_runs: tuple[PrematchWalkForwardRun, ...]
    final_models: tuple[PrematchFinalModel, ...]
    dependency_revision: int | None = None

    def __post_init__(self) -> None:
        if self.backtest_version != PREMATCH_BACKTEST_VERSION:
            raise ValueError("unsupported prematch backtest version")
        mode = AvailabilityMode(self.availability_mode).value
        object.__setattr__(self, "availability_mode", mode)
        cutoff = _utc(self.evaluation_cutoff, "evaluation_cutoff")
        object.__setattr__(self, "evaluation_cutoff", cutoff)
        if self.corpus.availability_mode != mode:
            raise ValueError("backtest result and corpus availability modes disagree")
        if self.dependency_revision != self.corpus.dependency_revision:
            raise ValueError("backtest result and corpus dependency revisions disagree")
        if self.dependency_revision is not None:
            _positive_int(self.dependency_revision, "dependency_revision")
        identities = tuple(
            (row.target.match_id, row.model_kind) for row in self.walk_forward_runs
        )
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate prematch walk-forward run identity")
        if any(
            row.target.prediction_cutoff >= cutoff for row in self.walk_forward_runs
        ):
            raise ValueError("evaluation cutoff must follow every target")
        final_kinds = tuple(row.model_artifact.model_kind for row in self.final_models)
        if final_kinds != PREMATCH_MODEL_KINDS:
            raise ValueError(
                "final prematch models do not match the fixed model family"
            )

    @property
    def model_artifacts(self) -> tuple[PrematchModelArtifact, ...]:
        return tuple(row.model_artifact for row in self.walk_forward_runs)

    @property
    def predictions(self) -> tuple[PrematchPrediction, ...]:
        return tuple(row.prediction for row in self.walk_forward_runs)

    @property
    def calibration_samples(self) -> tuple[PrematchCalibrationSample, ...]:
        return tuple(
            row.calibration_sample
            for row in self.walk_forward_runs
            if row.calibration_sample is not None
        )

    @property
    def calibration_artifacts(self) -> tuple[PrematchCalibrationArtifact, ...]:
        return tuple(row.calibration_artifact for row in self.final_models)


@dataclass(frozen=True)
class PrematchBacktestPersistenceResult:
    """Persistence records and controlled settlement counts for one execution."""

    counts: object
    settled_predictions: int
    unchanged_settlements: int
    model_runs: tuple[object, ...]
    calibration_artifacts: tuple[object, ...]
    predictions: tuple[object, ...]
    validations: tuple[object, ...]


def _connection(
    storage_or_connection: IntelligenceStorage | PostgresSession,
) -> PostgresSession:
    if isinstance(storage_or_connection, IntelligenceStorage):
        return storage_or_connection.connection
    if isinstance(storage_or_connection, PostgresSession):
        return storage_or_connection
    raise ValueError("storage_or_connection must provide PostgreSQL authority")


def _load_rosh_authority(
    connection: PostgresSession,
) -> tuple[tuple[StoredRoshRun, ...], tuple[RoshRunMatchLink, ...]]:
    repository = RoshRunRepository(connection)
    rows = connection.execute(
        "SELECT run_id FROM rosh_analysis_runs ORDER BY run_id"
    ).fetchall()
    runs: list[StoredRoshRun] = []
    links: list[RoshRunMatchLink] = []
    for row in rows:
        run_id = str(row["run_id"])
        stored = repository.get(run_id)
        if stored is None:
            raise ValueError(f"R.O.S.H. run authority disappeared: {run_id}")
        runs.append(stored)
        links.extend(repository.get_match_links(run_id))
    return tuple(runs), tuple(links)


def _prematch_dependency_revision(connection: PostgresSession) -> int | None:
    relation = connection.execute(
        "SELECT to_regclass('prematch_lineage_revisions') AS relation"
    ).fetchone()
    if relation is None or relation["relation"] is None:
        return None
    row = connection.execute(
        """SELECT dependency_revision
             FROM prematch_lineage_revisions
            WHERE singleton=1"""
    ).fetchone()
    if row is None:
        raise ValueError("prematch lineage revision authority is unavailable")
    return _positive_int(int(row["dependency_revision"]), "dependency_revision")


def _heroes_by_expected_position(team: DraftTeam) -> tuple[int, ...] | None:
    by_position: dict[int, int] = {}
    for player in team.players:
        position = player.expected_position
        if (
            position is None
            or player.expected_position_confidence < ROLE_CONFIDENCE_MIN
            or position in by_position
        ):
            return None
        by_position[position] = player.hero_id
    if set(by_position) != set(range(1, 6)):
        return None
    return tuple(by_position[position] for position in range(1, 6))


_CLUSTER_POSITION = {
    1: ("core", "safe"),
    2: ("core", "mid"),
    3: ("core", "off"),
    4: ("support", "off"),
    5: ("support", "safe"),
}


def _cluster_players(team: DraftTeam) -> tuple[ClusterPlayer, ...]:
    players: list[ClusterPlayer] = []
    for player in team.players:
        role_lane = _CLUSTER_POSITION.get(player.expected_position)
        confidence = player.expected_position_confidence
        players.append(
            ClusterPlayer(
                hero_id=player.hero_id,
                expected_role=None if role_lane is None else role_lane[0],
                expected_lane=None if role_lane is None else role_lane[1],
                role_confidence=confidence,
                lane_confidence=confidence,
            )
        )
    return tuple(players)


def _build_cluster_artifact(
    draft_target: DraftTarget,
    resource: ClusterResource,
) -> ClusterFeatureArtifact:
    return build_cluster_feature_artifact(
        ClusterFeatureTarget(
            match_id=draft_target.match_id,
            prediction_cutoff=draft_target.prediction_cutoff,
            patch=str(draft_target.patch),
            evidence_mode=ClusterEvidenceMode.PUBLISHED_STATIC,
            radiant=_cluster_players(draft_target.radiant),
            dire=_cluster_players(draft_target.dire),
        ),
        resource,
    )


def _build_rosh_snapshot_with_authority(
    draft_target: DraftTarget,
    rosh_runs: Iterable[StoredRoshRun],
    *,
    artifact_root: str | Path,
    match_links: Iterable[RoshRunMatchLink],
    bridge_witnesses: Mapping[int, RoshRequestPlanWitness] | None = None,
) -> tuple[RoshFeatureSnapshot, dict[str, object]]:
    radiant_heroes = _heroes_by_expected_position(draft_target.radiant)
    dire_heroes = _heroes_by_expected_position(draft_target.dire)
    if radiant_heroes is None or dire_heroes is None:
        return build_unavailable_rosh_feature_snapshot_with_authority(
            match_id=draft_target.match_id,
            prediction_cutoff=draft_target.prediction_cutoff,
            availability_mode=draft_target.availability_mode,
            radiant_hero_ids=(
                player.hero_id for player in draft_target.radiant.players
            ),
            dire_hero_ids=(player.hero_id for player in draft_target.dire.players),
        )
    witness = None if bridge_witnesses is None else bridge_witnesses.get(draft_target.match_id)
    selected_run = next(
        (
            stored
            for stored in rosh_runs
            if witness is not None and stored.run.run_id == witness.run_id
        ),
        None,
    )
    rosh_target = RoshFeatureTarget(
        match_id=draft_target.match_id,
        date_time=(
            int(draft_target.prediction_cutoff.timestamp())
            if selected_run is None
            else selected_run.run.date_time
        ),
        prediction_cutoff=draft_target.prediction_cutoff,
        availability_mode=draft_target.availability_mode.value,
        radiant_hero_ids=radiant_heroes,
        dire_hero_ids=dire_heroes,
    )
    return build_rosh_feature_snapshot_with_authority(
        rosh_target,
        rosh_runs,
        artifact_root=artifact_root,
        match_links=match_links,
        run_id=None if witness is None else witness.run_id,
        request_plan_witness=witness,
    )


def _draft_targets(corpus: DraftCorpus) -> dict[int, LoadedDraftMap]:
    result: dict[int, LoadedDraftMap] = {}
    for row in corpus.targets:
        existing = result.get(row.match_id)
        if existing is not None and existing != row:
            raise ValueError(f"conflicting Draft target authority for {row.match_id}")
        result[row.match_id] = row
    return result


def _unavailable_target(
    run: TeamRatingWalkForwardRun,
    rating_map: RatingMapInput,
    *,
    patch_id: str | None,
    reason: str,
) -> PrematchBacktestTarget:
    prediction = run.artifact.prediction
    return PrematchBacktestTarget(
        match_id=rating_map.match_id,
        series_id=run.series_id,
        event_id=run.event_id,
        patch_id=patch_id,
        prediction_cutoff=prediction.prediction_cutoff,
        completed_at=rating_map.completed_at,
        result_usable_at=rating_map.result_usable_at,
        cutoff_source=run.cutoff_source,
        availability_mode=run.availability_mode,
        outcome=run.eventual_radiant_win,
        team_base_probability=prediction.raw_probability,
        radiant_prior_probability=run.radiant_prior_probability,
        snapshot=None,
        failure_reason=reason,
    )


def load_prematch_corpus(
    storage_or_connection: IntelligenceStorage | PostgresSession,
    *,
    artifact_root: str | Path,
    availability_mode: AvailabilityMode,
    cluster_resource: ClusterResource | None = None,
    max_maps: int | None = None,
) -> PrematchCorpus:
    """Rebuild M2-M5 authority for every formal target; never accept snapshots."""

    if not isinstance(availability_mode, AvailabilityMode):
        raise ValueError("availability_mode must be an AvailabilityMode")
    if max_maps is not None and (
        isinstance(max_maps, bool) or not isinstance(max_maps, int) or max_maps < 1
    ):
        raise ValueError("max_maps must be a positive integer")
    connection = _connection(storage_or_connection)
    published_cluster_resource = (
        None
        if availability_mode is AvailabilityMode.RECONSTRUCTED
        else cluster_resource or load_cluster_resource()
    )
    # Keep the reads atomic with the caller and detect READ COMMITTED revision
    # drift explicitly. PostgresSession uses a savepoint for an existing
    # transaction; before the M6 migration, the revision table is absent.
    with connection.transaction():
        revision_before = _prematch_dependency_revision(connection)
        team_corpus = load_team_rating_corpus(
            connection,
            availability_mode=availability_mode,
        )
        draft_corpus = load_draft_corpus(
            connection,
            availability_mode=availability_mode,
            assignment_version=(
                RECONSTRUCTED_ASSIGNMENT_VERSION
                if availability_mode is AvailabilityMode.RECONSTRUCTED
                else PROSPECTIVE_ASSIGNMENT_VERSION
            ),
        )
        rosh_runs, rosh_links = _load_rosh_authority(connection)
        rosh_bridge_witnesses = (
            load_rosh_bridge_witnesses(connection)
            if isinstance(connection, PostgresSession)
            else {}
        )
        revision_after = _prematch_dependency_revision(connection)
        if revision_before != revision_after:
            raise ValueError("prematch authority changed during corpus load")
    if max_maps is not None:
        selected_team_maps = team_corpus.maps[:max_maps]
        team_corpus = TeamRatingCorpus(
            team_corpus.availability_mode,
            len(selected_team_maps),
            selected_team_maps,
        )
        selected_match_ids = {
            loaded.row.match_id for loaded in selected_team_maps
        }
        selected_draft_maps = tuple(
            row for row in draft_corpus.maps if row.match_id in selected_match_ids
        )
        draft_corpus = replace(
            draft_corpus,
            formal_draft_maps=len(selected_draft_maps),
            maps=selected_draft_maps,
            profile_maps=tuple(
                row
                for row in draft_corpus.profile_maps
                if row.state.match_id in selected_match_ids
            ),
        )
    # Nested Team Rating selection and all feature replay are CPU-bound and
    # operate on the immutable dataclasses loaded above.
    team_runs = build_team_rating_walk_forward_runs(team_corpus)
    team_rating_evidence_cache: TeamRatingResidualEvidenceCache = (
        build_team_rating_residual_evidence_cache(team_runs)
    )
    team_maps = {loaded.row.match_id: loaded.row for loaded in team_corpus.maps}
    draft_by_match = _draft_targets(draft_corpus)
    draft_history = tuple(row.evidence for row in draft_corpus.maps)

    targets: list[PrematchBacktestTarget] = []
    for team_run in team_runs:
        rating_target = team_run.artifact.target
        rating_prediction = team_run.artifact.prediction
        rating_map = team_maps.get(rating_target.match_id)
        if rating_map is None:
            raise ValueError("Team Rating target lacks its formal map authority")
        if rating_map.radiant_win != team_run.eventual_radiant_win:
            raise ValueError("Team Rating target and map outcomes disagree")
        draft_row = draft_by_match.get(rating_target.match_id)
        draft_target = None if draft_row is None else draft_row.target
        patch_id = (
            None
            if draft_target is None or draft_target.patch is None
            else str(draft_target.patch)
        )
        if team_run.status != "trained":
            targets.append(
                _unavailable_target(
                    team_run,
                    rating_map,
                    patch_id=patch_id,
                    reason="team_rating_insufficient_evidence",
                )
            )
            continue
        if draft_target is None:
            targets.append(
                _unavailable_target(
                    team_run,
                    rating_map,
                    patch_id=None,
                    reason="draft_target_unavailable",
                )
            )
            continue
        if patch_id is None:
            targets.append(
                _unavailable_target(
                    team_run,
                    rating_map,
                    patch_id=None,
                    reason="patch_identity_unavailable",
                )
            )
            continue
        if (
            draft_target.prediction_cutoff != rating_prediction.prediction_cutoff
            or draft_target.event_id != team_run.event_id
            or draft_target.availability_mode.value != team_run.availability_mode
            or draft_target.radiant.team_id != rating_target.radiant_team_id
            or draft_target.dire.team_id != rating_target.dire_team_id
        ):
            raise ValueError("M2 and Draft target identities disagree")
        if (
            draft_target.series_id is not None
            and team_run.series_id is not None
            and draft_target.series_id != team_run.series_id
        ):
            raise ValueError("M2 and Draft series identities disagree")

        draft_snapshot, draft_authority = build_draft_residual_snapshot_with_authority(
            draft_target,
            draft_history,
            target_team_rating=team_run,
            team_rating_history=team_runs,
            team_rating_evidence_cache=team_rating_evidence_cache,
        )
        rosh_snapshot, rosh_authority = _build_rosh_snapshot_with_authority(
            draft_target,
            rosh_runs,
            artifact_root=artifact_root,
            match_links=rosh_links,
            bridge_witnesses=rosh_bridge_witnesses,
        )
        cluster_artifact = (
            None
            if published_cluster_resource is None
            else _build_cluster_artifact(draft_target, published_cluster_resource)
        )
        snapshot = build_prematch_feature_snapshot(
            draft_authority,
            rosh_authority,
            target_team_rating=team_run,
            team_rating_history=team_runs,
            rosh_runs=rosh_runs,
            artifact_root=artifact_root,
            match_links=rosh_links,
            team_rating_evidence_cache=team_rating_evidence_cache,
            cluster_artifact=cluster_artifact,
        )
        if (
            snapshot.draft_residual_input_hash != draft_snapshot.input_hash
            or snapshot.rosh_input_hash != rosh_snapshot.input_hash
        ):
            raise ValueError("M5 replay does not match the rebuilt M3/M4 snapshots")
        targets.append(
            PrematchBacktestTarget(
                match_id=rating_target.match_id,
                series_id=team_run.series_id,
                event_id=team_run.event_id,
                patch_id=patch_id,
                prediction_cutoff=rating_prediction.prediction_cutoff,
                completed_at=rating_map.completed_at,
                result_usable_at=rating_map.result_usable_at,
                cutoff_source=team_run.cutoff_source,
                availability_mode=team_run.availability_mode,
                outcome=team_run.eventual_radiant_win,
                team_base_probability=rating_prediction.raw_probability,
                radiant_prior_probability=team_run.radiant_prior_probability,
                snapshot=snapshot,
                failure_reason=None,
            )
        )

    return PrematchCorpus(
        availability_mode=availability_mode.value,
        formal_maps=team_corpus.formal_maps,
        targets=tuple(
            sorted(targets, key=lambda row: (row.prediction_cutoff, row.match_id))
        ),
        team_rating_runs=team_runs,
        draft_corpus=draft_corpus,
        rosh_runs=rosh_runs,
        rosh_match_links=rosh_links,
        dependency_revision=revision_after,
    )


def _training_row(
    target: PrematchBacktestTarget,
    model_kind: str,
) -> PrematchTrainingRow:
    if target.snapshot is None or target.patch_id is None:
        raise ValueError("training rows require an available prematch target")
    return PrematchTrainingRow.from_snapshot(
        target.snapshot,
        model_kind=model_kind,
        completed_at=target.completed_at,
        result_usable_at=target.result_usable_at,
        outcome=target.outcome,
        series_id=_series_key(target.series_id, target.match_id),
        event_id=target.event_id,
        patch_id=target.patch_id,
    )


def _snapshot_supports_model(
    snapshot: PrematchFeatureSnapshot,
    model_kind: str,
) -> bool:
    return model_kind != CLUSTER_MODEL_KIND or (
        snapshot.cluster_artifact is not None and snapshot.cluster_coverage > 0.0
    )


def _calibration_sample(
    target: PrematchBacktestTarget,
    model_kind: str,
    model: PrematchModelArtifact,
    prediction: PrematchPrediction,
) -> PrematchCalibrationSample | None:
    if (
        prediction.status is not PredictionStatus.PREDICTED
        or prediction.raw_probability is None
        or target.result_usable_at is None
        or target.snapshot is None
        or target.patch_id is None
    ):
        return None
    return PrematchCalibrationSample(
        match_id=target.match_id,
        series_id=_series_key(target.series_id, target.match_id),
        event_id=target.event_id,
        patch_id=target.patch_id,
        model_kind=model_kind,
        availability_mode=target.availability_mode,
        prediction_cutoff=target.prediction_cutoff,
        result_usable_at=target.result_usable_at,
        raw_probability=prediction.raw_probability,
        outcome=int(target.outcome),
        model_hash=model.model_hash,
        input_snapshot_hash=target.snapshot.input_hash,
    )


def _evaluation_cutoff(targets: Iterable[PrematchBacktestTarget]) -> datetime:
    values = tuple(targets)
    if not values:
        return _EPOCH + timedelta(microseconds=1)
    latest = max(row.result_usable_at or row.completed_at for row in values)
    return max(latest, max(row.prediction_cutoff for row in values)) + timedelta(
        microseconds=1
    )


def build_prematch_walk_forward(
    corpus: PrematchCorpus,
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    l2_regularization: float = DEFAULT_L2_REGULARIZATION,
) -> PrematchBacktestResult:
    """Fit every target from earlier-only rows and retain the complete OOS stream."""

    if not isinstance(corpus, PrematchCorpus):
        raise ValueError("corpus must be a PrematchCorpus")
    histories: dict[str, list[PrematchTrainingRow]] = {
        kind: [] for kind in PREMATCH_MODEL_KINDS
    }
    runs: list[PrematchWalkForwardRun] = []
    for target in corpus.targets:
        if target.snapshot is None:
            continue
        for model_kind in PREMATCH_MODEL_KINDS:
            if not _snapshot_supports_model(target.snapshot, model_kind):
                continue
            model = fit_prematch_model(
                histories[model_kind],
                target.prediction_cutoff,
                model_kind=model_kind,
                availability_mode=corpus.availability_mode,
                min_samples=min_samples,
                l2_regularization=l2_regularization,
            )
            prediction = predict_prematch(model, target.snapshot)
            runs.append(
                PrematchWalkForwardRun(
                    target=target,
                    model_kind=model_kind,
                    model_artifact=model,
                    prediction=prediction,
                    calibration_sample=_calibration_sample(
                        target,
                        model_kind,
                        model,
                        prediction,
                    ),
                )
            )
        for model_kind in PREMATCH_MODEL_KINDS:
            if _snapshot_supports_model(target.snapshot, model_kind):
                histories[model_kind].append(_training_row(target, model_kind))

    cutoff = _evaluation_cutoff(corpus.targets)
    samples_by_kind: dict[str, list[PrematchCalibrationSample]] = {
        kind: [] for kind in PREMATCH_MODEL_KINDS
    }
    for run in runs:
        if run.calibration_sample is not None:
            samples_by_kind[run.model_kind].append(run.calibration_sample)

    final_models: list[PrematchFinalModel] = []
    for model_kind in PREMATCH_MODEL_KINDS:
        model = fit_prematch_model(
            histories[model_kind],
            cutoff,
            model_kind=model_kind,
            availability_mode=corpus.availability_mode,
            min_samples=min_samples,
            l2_regularization=l2_regularization,
        )
        calibration = build_prematch_calibration_artifact(
            samples_by_kind[model_kind],
            cutoff,
            model_kind=model_kind,
            availability_mode=corpus.availability_mode,
        )
        final_models.append(PrematchFinalModel(model, calibration))

    return PrematchBacktestResult(
        backtest_version=PREMATCH_BACKTEST_VERSION,
        availability_mode=corpus.availability_mode,
        evaluation_cutoff=cutoff,
        corpus=corpus,
        walk_forward_runs=tuple(runs),
        final_models=tuple(final_models),
        dependency_revision=corpus.dependency_revision,
    )


def run_prematch_backtest(
    storage_or_connection: IntelligenceStorage | PostgresSession,
    *,
    artifact_root: str | Path,
    availability_mode: AvailabilityMode = AvailabilityMode.RECONSTRUCTED,
    max_maps: int | None = None,
) -> PrematchBacktestResult:
    """Load formal authority and execute M6 without persistence side effects."""

    corpus = load_prematch_corpus(
        storage_or_connection,
        artifact_root=artifact_root,
        availability_mode=availability_mode,
        max_maps=max_maps,
    )
    return build_prematch_walk_forward(corpus)


def persist_prematch_backtest_result(
    result: PrematchBacktestResult,
    storage_or_connection: IntelligenceStorage | PostgresSession,
    *,
    report: PrematchBacktestReport,
    dry_run: bool = False,
    created_at: datetime | None = None,
    validated_at: datetime | None = None,
) -> PrematchBacktestPersistenceResult:
    """Persist all M6 evidence and settle reconstructed rows atomically.

    OOS predictions intentionally receive no final calibration identity: the
    final refit calibrator belongs to its final model hash and is persisted as a
    separate artifact. Settlement is only legal for reconstructed evidence and
    is performed through the controlled storage transition.
    """

    if not isinstance(result, PrematchBacktestResult):
        raise ValueError("result must be a PrematchBacktestResult")
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be boolean")
    connection = _connection(storage_or_connection)
    from .prematch_report import (
        PREMATCH_BOOTSTRAP_SAMPLES,
        PrematchBacktestReport,
        prematch_model_run_metrics,
    )
    from .prematch_storage import (
        PrematchCorpusStore,
        build_prematch_calibration_record,
        build_prematch_model_run_record,
        build_prematch_prediction_record,
        build_prematch_validation_record,
        persist_prematch_records,
        require_prematch_dependency_revision_current,
        settle_prematch_prediction,
    )

    if not isinstance(report, PrematchBacktestReport):
        raise ValueError("report must be a PrematchBacktestReport")
    if (
        report.backtest_version != result.backtest_version
        or report.availability_mode != result.availability_mode
        or report.formal_maps != result.corpus.formal_maps
        or report.eligible_targets != result.corpus.eligible_targets
        or report.bootstrap_samples != PREMATCH_BOOTSTRAP_SAMPLES
    ):
        raise ValueError("prematch report does not match the formal result")
    expected_calibrations = tuple(
        (
            row.model_artifact.model_kind,
            row.calibration_artifact.calibration_hash,
        )
        for row in result.final_models
    )
    actual_calibrations = tuple(
        (row.model_kind, row.calibration_hash) for row in report.calibration
    )
    if actual_calibrations != expected_calibrations:
        raise ValueError("prematch report calibration identities disagree")

    dependency_revision = result.dependency_revision
    if dependency_revision is None:
        raise ValueError("prematch result lacks its load-time dependency revision")
    validation_time = (
        result.evaluation_cutoff
        if validated_at is None
        else _utc(validated_at, "validated_at")
    )
    model_runs_by_hash: dict[str, object] = {}
    corpus_store = PrematchCorpusStore()
    prediction_records: list[object] = []
    validation_records: list[object] = []
    target_by_identity = {
        (run.model_artifact.model_hash, run.target.match_id): run.target
        for run in result.walk_forward_runs
    }
    for run in result.walk_forward_runs:
        model_record = build_prematch_model_run_record(
            run.model_artifact,
            corpus_store=corpus_store,
        )
        model_runs_by_hash[model_record.run_id] = model_record
        prediction_record = build_prematch_prediction_record(
            run.model_artifact,
            run.target.snapshot,
            cutoff_source=run.target.cutoff_source,
            dependency_revision=dependency_revision,
        )
        prediction_records.append(prediction_record)
        validation_records.append(
            build_prematch_validation_record(
                model_record,
                prediction_record,
                validated_at=validation_time,
            )
        )

    calibration_records: list[object] = []
    for final in result.final_models:
        model_record = build_prematch_model_run_record(
            final.model_artifact,
            metrics=prematch_model_run_metrics(
                report,
                final.model_artifact.model_kind,
            ),
            corpus_store=corpus_store,
        )
        model_runs_by_hash[model_record.run_id] = model_record
        calibration_records.append(
            build_prematch_calibration_record(
                final.calibration_artifact,
                model_hash=final.model_artifact.model_hash,
            )
        )

    with connection.transaction():
        require_prematch_dependency_revision_current(
            connection,
            dependency_revision=dependency_revision,
            evaluation_cutoff=result.evaluation_cutoff,
        )
        counts = persist_prematch_records(
            connection,
            model_runs=tuple(model_runs_by_hash.values()),
            calibration_artifacts=tuple(calibration_records),
            predictions=tuple(prediction_records),
            validations=tuple(validation_records),
            dry_run=dry_run,
            created_at=created_at,
        )
        settled = 0
        unchanged = 0
        if (
            not dry_run
            and result.availability_mode == AvailabilityMode.RECONSTRUCTED.value
        ):
            for prediction in prediction_records:
                target = target_by_identity.get(
                    (prediction.run_id, prediction.match_id)
                )
                if (
                    target is None
                    or prediction.status != "predicted"
                    or target.result_usable_at is None
                ):
                    continue
                settlement = settle_prematch_prediction(
                    connection,
                    run_id=prediction.run_id,
                    match_id=prediction.match_id,
                    eventual_radiant_win=target.outcome,
                    result_usable_at=target.result_usable_at,
                    settled_at=target.result_usable_at,
                )
                if settlement.updated:
                    settled += 1
                elif settlement.unchanged:
                    unchanged += 1

    return PrematchBacktestPersistenceResult(
        counts=counts,
        settled_predictions=settled,
        unchanged_settlements=unchanged,
        model_runs=tuple(model_runs_by_hash.values()),
        calibration_artifacts=tuple(calibration_records),
        predictions=tuple(prediction_records),
        validations=tuple(validation_records),
    )


__all__ = [
    "CLUSTER_MODEL_KIND",
    "PREMATCH_BACKTEST_VERSION",
    "PrematchBacktestResult",
    "PrematchBacktestTarget",
    "PrematchCorpus",
    "PrematchFinalModel",
    "PrematchBacktestPersistenceResult",
    "PrematchWalkForwardRun",
    "build_prematch_walk_forward",
    "load_prematch_corpus",
    "persist_prematch_backtest_result",
    "run_prematch_backtest",
]
