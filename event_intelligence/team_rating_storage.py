"""Immutable PostgreSQL persistence for Team Rating walk-forward evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from database.session import DatabaseRow, PostgresSession

from .raw_archive import canonical_json_bytes
from .team_rating import TEAM_RATING_VERSION, TeamRatingState
from .team_rating_artifacts import TeamRatingArtifact, verify_team_rating_artifact
from .team_rating_backtest import (
    combined_team_rating_training_input_hash,
    team_rating_authority_fingerprint,
    team_rating_run_id,
)

if TYPE_CHECKING:
    from .team_rating_backtest import TeamRatingWalkForwardRun


UTC = timezone.utc
_AVAILABILITY_MODES = frozenset(
    {"reconstructed_walk_forward", "prospective"}
)
_RUN_STATUSES = frozenset({"trained", "insufficient_evidence", "failed"})
_PREDICTION_STATUSES = frozenset(
    {"predicted", "insufficient_evidence", "settled", "failed"}
)


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


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _finite(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{field} must be finite")
    return float(value)


def _probability(value: object, field: str) -> float:
    result = _finite(value, field)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{field} must be between 0 and 1")
    return result


def _optional_probability(value: object, field: str) -> float | None:
    return None if value is None else _probability(value, field)


def _canonical_object_json(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be canonical JSON")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{field} must be canonical JSON") from error
    if not isinstance(parsed, dict) or _canonical_json(parsed) != value:
        raise ValueError(f"{field} must be a canonical JSON object")
    return value


def _canonical_roster_json(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("roster_json must be canonical JSON")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("roster_json must be canonical JSON") from error
    if not isinstance(parsed, list) or _canonical_json(parsed) != value:
        raise ValueError("roster_json must be a canonical JSON array")
    if len(parsed) not in (0, 5):
        raise ValueError("roster_json must be empty or contain five players")
    if any(type(player_id) is not int or player_id <= 0 for player_id in parsed):
        raise ValueError("roster_json player IDs must be positive integers")
    if len(set(parsed)) != len(parsed):
        raise ValueError("roster_json player IDs must be unique")
    return value


@dataclass(frozen=True)
class TeamRatingRunRecord:
    run_id: str
    rating_version: str
    artifact_version: str
    availability_mode: str
    training_cutoff: datetime
    configuration_json: str
    training_input_hash: str
    metrics_json: str | None
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _sha256(self.run_id, "run_id"))
        _nonempty(self.rating_version, "rating_version")
        _nonempty(self.artifact_version, "artifact_version")
        if self.availability_mode not in _AVAILABILITY_MODES:
            raise ValueError("unsupported Team Rating availability mode")
        object.__setattr__(
            self,
            "training_cutoff",
            _utc(self.training_cutoff, "training_cutoff"),
        )
        object.__setattr__(
            self,
            "configuration_json",
            _canonical_object_json(self.configuration_json, "configuration_json"),
        )
        object.__setattr__(
            self,
            "training_input_hash",
            _sha256(self.training_input_hash, "training_input_hash"),
        )
        if self.metrics_json is not None:
            object.__setattr__(
                self,
                "metrics_json",
                _canonical_object_json(self.metrics_json, "metrics_json"),
            )
        if self.status not in _RUN_STATUSES:
            raise ValueError("unsupported Team Rating run status")

    def stable_columns(self) -> tuple[object, ...]:
        return (
            self.rating_version,
            self.artifact_version,
            self.availability_mode,
            self.training_cutoff.isoformat(),
            self.configuration_json,
            self.training_input_hash,
            self.metrics_json,
            self.status,
        )


@dataclass(frozen=True)
class TeamRatingPredictionRecord:
    run_id: str
    match_id: int
    prediction_cutoff: datetime
    cutoff_source: str
    radiant_team_id: int
    dire_team_id: int
    radiant_rating: float
    dire_rating: float
    rating_diff: float
    raw_probability: float | None
    radiant_roster_continuity: float | None
    dire_roster_continuity: float | None
    support: int
    input_hash: str
    eventual_radiant_win: bool | None
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
        radiant_team_id = _positive_int(self.radiant_team_id, "radiant_team_id")
        dire_team_id = _positive_int(self.dire_team_id, "dire_team_id")
        if radiant_team_id == dire_team_id:
            raise ValueError("radiant and dire team IDs must differ")
        radiant = _finite(self.radiant_rating, "radiant_rating")
        dire = _finite(self.dire_rating, "dire_rating")
        difference = _finite(self.rating_diff, "rating_diff")
        if difference != radiant - dire:
            raise ValueError("rating_diff does not match team ratings")
        object.__setattr__(self, "radiant_rating", radiant)
        object.__setattr__(self, "dire_rating", dire)
        object.__setattr__(self, "rating_diff", difference)
        object.__setattr__(
            self,
            "raw_probability",
            _optional_probability(self.raw_probability, "raw_probability"),
        )
        object.__setattr__(
            self,
            "radiant_roster_continuity",
            _optional_probability(
                self.radiant_roster_continuity,
                "radiant_roster_continuity",
            ),
        )
        object.__setattr__(
            self,
            "dire_roster_continuity",
            _optional_probability(
                self.dire_roster_continuity,
                "dire_roster_continuity",
            ),
        )
        _nonnegative_int(self.support, "support")
        object.__setattr__(
            self,
            "input_hash",
            _sha256(self.input_hash, "input_hash"),
        )
        if self.eventual_radiant_win is not None and not isinstance(
            self.eventual_radiant_win, bool
        ):
            raise ValueError("eventual_radiant_win must be boolean or None")
        if self.status not in _PREDICTION_STATUSES:
            raise ValueError("unsupported Team Rating prediction status")
        if (self.status == "settled") != (self.eventual_radiant_win is not None):
            raise ValueError("only settled predictions may contain an outcome")
        if (self.status in {"predicted", "settled"}) != (
            self.raw_probability is not None
        ):
            raise ValueError(
                "only predicted or settled predictions may contain a probability"
            )

    def stable_columns(self) -> tuple[object, ...]:
        return (
            self.match_id,
            self.prediction_cutoff.isoformat(),
            self.cutoff_source,
            self.radiant_team_id,
            self.dire_team_id,
            self.radiant_rating,
            self.dire_rating,
            self.rating_diff,
            self.raw_probability,
            self.radiant_roster_continuity,
            self.dire_roster_continuity,
            self.support,
            self.input_hash,
            (
                None
                if self.eventual_radiant_win is None
                else int(self.eventual_radiant_win)
            ),
            self.status,
        )


@dataclass(frozen=True)
class TeamRatingStateSnapshotRecord:
    snapshot_key: str
    run_id: str
    as_of: datetime
    team_id: int
    rating: float
    maps_seen: int
    roster_json: str
    last_observed_at: datetime | None
    state_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "snapshot_key",
            _sha256(self.snapshot_key, "snapshot_key"),
        )
        object.__setattr__(self, "run_id", _sha256(self.run_id, "run_id"))
        as_of = _utc(self.as_of, "as_of")
        object.__setattr__(self, "as_of", as_of)
        _positive_int(self.team_id, "team_id")
        object.__setattr__(self, "rating", _finite(self.rating, "rating"))
        _nonnegative_int(self.maps_seen, "maps_seen")
        object.__setattr__(
            self,
            "roster_json",
            _canonical_roster_json(self.roster_json),
        )
        if self.last_observed_at is not None:
            observed = _utc(self.last_observed_at, "last_observed_at")
            if observed > as_of:
                raise ValueError("last_observed_at cannot follow as_of")
            object.__setattr__(self, "last_observed_at", observed)
        object.__setattr__(
            self,
            "state_hash",
            _sha256(self.state_hash, "state_hash"),
        )

    def stable_columns(self) -> tuple[object, ...]:
        return (
            self.snapshot_key,
            self.rating,
            self.maps_seen,
            self.roster_json,
            (
                None
                if self.last_observed_at is None
                else self.last_observed_at.isoformat()
            ),
            self.state_hash,
        )


@dataclass(frozen=True)
class TeamRatingPersistenceCounts:
    inserted_runs: int = 0
    unchanged_runs: int = 0
    inserted_predictions: int = 0
    unchanged_predictions: int = 0
    inserted_snapshots: int = 0
    unchanged_snapshots: int = 0


TeamRatingStorageRecords = tuple[
    TeamRatingRunRecord,
    TeamRatingPredictionRecord,
    tuple[TeamRatingStateSnapshotRecord, ...],
]


def _normalize_storage_records(
    records: Sequence[TeamRatingStorageRecords],
) -> tuple[TeamRatingStorageRecords, ...]:
    by_run_id: dict[str, TeamRatingStorageRecords] = {}
    normalized: list[TeamRatingStorageRecords] = []
    for record in records:
        run_record, prediction_record, snapshot_records = record
        existing = by_run_id.get(run_record.run_id)
        if existing is None:
            by_run_id[run_record.run_id] = record
            normalized.append(record)
            continue
        existing_run, existing_prediction, existing_snapshots = existing
        if existing_run.stable_columns() != run_record.stable_columns():
            raise ValueError(
                f"immutable Team Rating run conflict: {run_record.run_id}"
            )
        if (
            existing_prediction.run_id,
            existing_prediction.match_id,
            existing_prediction.stable_columns(),
        ) != (
            prediction_record.run_id,
            prediction_record.match_id,
            prediction_record.stable_columns(),
        ):
            raise ValueError(
                "immutable Team Rating prediction conflict: "
                f"{prediction_record.run_id}/{prediction_record.match_id}"
            )
        existing_snapshot_values = tuple(
            (
                snapshot.run_id,
                snapshot.as_of,
                snapshot.team_id,
                snapshot.stable_columns(),
            )
            for snapshot in existing_snapshots
        )
        snapshot_values = tuple(
            (
                snapshot.run_id,
                snapshot.as_of,
                snapshot.team_id,
                snapshot.stable_columns(),
            )
            for snapshot in snapshot_records
        )
        if existing_snapshot_values != snapshot_values:
            raise ValueError(
                f"immutable Team Rating state snapshot conflict: {run_record.run_id}"
            )
    return tuple(normalized)


def _require_exact_row(
    existing: DatabaseRow | None,
    expected: tuple[object, ...],
    conflict: str,
) -> None:
    if existing is None:
        raise RuntimeError("Team Rating identity conflict row is unavailable")
    if tuple(existing) != expected:
        raise ValueError(conflict)


def _state_snapshot_record(
    run_id: str,
    as_of: datetime,
    state: TeamRatingState,
) -> TeamRatingStateSnapshotRecord:
    state_payload = state.to_payload()
    state_hash = _hash(
        {
            "rating_version": TEAM_RATING_VERSION,
            "state": state_payload,
        }
    )
    snapshot_key = _hash(
        {
            "schema": "team-rating-state-snapshot/v1",
            "run_id": run_id,
            "as_of": as_of.isoformat(),
            "team_id": state.team_id,
            "state_hash": state_hash,
        }
    )
    return TeamRatingStateSnapshotRecord(
        snapshot_key=snapshot_key,
        run_id=run_id,
        as_of=as_of,
        team_id=state.team_id,
        rating=state.rating,
        maps_seen=state.maps_seen,
        roster_json=_canonical_json(list(state.roster)),
        last_observed_at=state.last_observed_at,
        state_hash=state_hash,
    )


def build_team_rating_storage_records(
    run: TeamRatingWalkForwardRun,
) -> tuple[
    TeamRatingRunRecord,
    TeamRatingPredictionRecord,
    tuple[TeamRatingStateSnapshotRecord, ...],
]:
    """Validate one walk-forward artifact and project its immutable DB rows."""

    artifact = getattr(run, "artifact", None)
    if not isinstance(artifact, TeamRatingArtifact):
        raise ValueError("walk-forward run must contain a TeamRatingArtifact")
    verify_team_rating_artifact(artifact)
    if getattr(run, "config", None) != artifact.config:
        raise ValueError("walk-forward run config does not match its artifact")
    mode = getattr(run, "availability_mode", None)
    cutoff_source = _nonempty(getattr(run, "cutoff_source", None), "cutoff_source")
    if mode != "reconstructed_walk_forward" or cutoff_source != (
        "reconstructed_map_start"
    ):
        raise ValueError(
            "M2 Team Rating persistence requires reconstructed map-start authority"
        )
    status = getattr(run, "status", None)
    if status not in _RUN_STATUSES:
        raise ValueError("unsupported Team Rating run status")

    target_source = getattr(run, "target_source_authority", None)
    try:
        ordered_training_sources = tuple(
            getattr(run, "ordered_training_sources", None)
        )
    except TypeError as error:
        raise ValueError("walk-forward run lacks ordered training source authority") from error
    target_match_id = getattr(target_source, "match_id", None)
    if target_match_id != artifact.target.match_id:
        raise ValueError("target source authority does not match artifact target")
    source_match_ids = tuple(
        getattr(source, "match_id", None) for source in ordered_training_sources
    )
    corpus_match_ids = tuple(row.match_id for row in artifact.ordered_training_corpus)
    if source_match_ids != corpus_match_ids:
        raise ValueError("training source manifest does not match artifact corpus")
    expected_authority_fingerprint = team_rating_authority_fingerprint(
        target_source=target_source,
        ordered_training_sources=ordered_training_sources,
    )
    claimed_authority_fingerprint = _sha256(
        getattr(run, "authority_fingerprint", None),
        "authority_fingerprint",
    )
    if not hmac.compare_digest(
        claimed_authority_fingerprint,
        expected_authority_fingerprint,
    ):
        raise ValueError("Team Rating authority fingerprint does not recompute")
    expected_training_input_hash = combined_team_rating_training_input_hash(
        artifact_training_input_hash=artifact.training_input_hash,
        authority_fingerprint=expected_authority_fingerprint,
    )
    claimed_training_input_hash = _sha256(
        getattr(run, "combined_training_input_hash", None),
        "combined_training_input_hash",
    )
    if not hmac.compare_digest(
        claimed_training_input_hash,
        expected_training_input_hash,
    ):
        raise ValueError("combined Team Rating training input hash does not recompute")
    expected_run_id = team_rating_run_id(
        availability_mode=mode,
        artifact_hash=artifact.artifact_hash,
        authority_fingerprint=expected_authority_fingerprint,
    )
    run_id = _sha256(getattr(run, "run_id", None), "run_id")
    if not hmac.compare_digest(run_id, expected_run_id):
        raise ValueError("Team Rating run_id does not recompute")

    selection = getattr(run, "selection", None)
    parameters = getattr(selection, "parameters", None)
    if selection is None or not callable(getattr(selection, "to_payload", None)):
        raise ValueError("walk-forward run lacks parameter selection evidence")
    if parameters is None or not callable(getattr(parameters, "to_payload", None)):
        raise ValueError("walk-forward run lacks selected parameters")
    selection_payload = selection.to_payload()
    parameter_payload = parameters.to_payload()
    if not isinstance(selection_payload, Mapping) or not isinstance(
        parameter_payload, Mapping
    ):
        raise ValueError("parameter selection payload must be an object")

    radiant_prior = _probability(
        getattr(run, "radiant_prior_probability", None),
        "radiant_prior_probability",
    )
    configuration_json = _canonical_json(
        {
            "artifact_hash": artifact.artifact_hash,
            "artifact_training_input_hash": artifact.training_input_hash,
            "authority_fingerprint": expected_authority_fingerprint,
            "config": artifact.config.to_payload(),
            "selected_parameters": dict(parameter_payload),
            "source_authority_manifest": {
                "target_source": target_source.to_payload(),
                "ordered_training_sources": [
                    source.to_payload() for source in ordered_training_sources
                ],
            },
        }
    )
    metrics_json = _canonical_json(
        {
            "parameter_selection": dict(selection_payload),
            "radiant_prior_probability": radiant_prior,
        }
    )
    run_record = TeamRatingRunRecord(
        run_id=run_id,
        rating_version=artifact.rating_version,
        artifact_version=artifact.artifact_version,
        availability_mode=str(mode),
        training_cutoff=artifact.training_cutoff,
        configuration_json=configuration_json,
        training_input_hash=expected_training_input_hash,
        metrics_json=metrics_json,
        status=str(status),
    )

    prediction = artifact.prediction
    target = artifact.target
    eventual = getattr(run, "eventual_radiant_win", None)
    if eventual is not None and not isinstance(eventual, bool):
        raise ValueError("eventual_radiant_win must be boolean or None")
    if prediction.match_id != target.match_id:
        raise ValueError("Team Rating prediction and target match IDs disagree")
    if status in {"insufficient_evidence", "failed"}:
        raw_probability = None
        stored_eventual = None
        prediction_status = str(status)
    else:
        raw_probability = prediction.raw_probability
        stored_eventual = eventual
        prediction_status = "settled" if eventual is not None else "predicted"
    prediction_record = TeamRatingPredictionRecord(
        run_id=run_id,
        match_id=prediction.match_id,
        prediction_cutoff=prediction.prediction_cutoff,
        cutoff_source=cutoff_source,
        radiant_team_id=target.radiant_team_id,
        dire_team_id=target.dire_team_id,
        radiant_rating=prediction.radiant_rating,
        dire_rating=prediction.dire_rating,
        rating_diff=prediction.rating_diff,
        raw_probability=raw_probability,
        radiant_roster_continuity=prediction.radiant_roster_continuity,
        dire_roster_continuity=prediction.dire_roster_continuity,
        support=prediction.support,
        input_hash=prediction.input_hash,
        eventual_radiant_win=stored_eventual,
        status=prediction_status,
    )
    snapshots = tuple(
        _state_snapshot_record(run_id, artifact.training_cutoff, state)
        for state in artifact.state_before_target
    )
    return run_record, prediction_record, snapshots


def _stored_run(
    connection: PostgresSession,
    record: TeamRatingRunRecord,
) -> DatabaseRow | None:
    return connection.execute(
        """SELECT rating_version, artifact_version, availability_mode,
                  training_cutoff, configuration_json,
                  training_input_hash, metrics_json, status
             FROM team_rating_runs WHERE run_id=?""",
        (record.run_id,),
    ).fetchone()


def _stored_prediction(
    connection: PostgresSession,
    record: TeamRatingPredictionRecord,
) -> DatabaseRow | None:
    return connection.execute(
        """SELECT match_id, prediction_cutoff, cutoff_source,
                  radiant_team_id, dire_team_id, radiant_rating,
                  dire_rating, rating_diff, raw_probability,
                  radiant_roster_continuity, dire_roster_continuity,
                  support, input_hash, eventual_radiant_win, status
             FROM team_rating_predictions
            WHERE run_id=? AND match_id=?""",
        (record.run_id, record.match_id),
    ).fetchone()


def _stored_snapshot(
    connection: PostgresSession,
    record: TeamRatingStateSnapshotRecord,
) -> DatabaseRow | None:
    return connection.execute(
        """SELECT snapshot_key, rating, maps_seen, roster_json,
                  last_observed_at, state_hash
             FROM team_rating_state_snapshots
            WHERE run_id=? AND as_of=? AND team_id=?""",
        (record.run_id, record.as_of.isoformat(), record.team_id),
    ).fetchone()


def persist_team_rating_runs(
    connection: PostgresSession,
    runs: Sequence[TeamRatingWalkForwardRun],
    *,
    dry_run: bool = False,
    checkpoint_run_ids: Collection[str] = (),
    created_at: datetime | None = None,
) -> TeamRatingPersistenceCounts:
    """Atomically insert exact Team Rating rows or reject identity conflicts."""

    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be boolean")
    created = _utc(
        datetime.now(UTC) if created_at is None else created_at,
        "created_at",
    ).isoformat()
    records = _normalize_storage_records(
        tuple(build_team_rating_storage_records(run) for run in runs)
    )
    requested_checkpoints = {
        _sha256(run_id, "checkpoint run_id") for run_id in checkpoint_run_ids
    }
    available_run_ids = {
        run_record.run_id for run_record, _prediction, _states in records
    }
    unknown = sorted(requested_checkpoints - available_run_ids)
    if unknown:
        raise ValueError(
            "checkpoint run IDs are not present in this batch: " + ", ".join(unknown)
        )

    inserted_runs = unchanged_runs = 0
    inserted_predictions = unchanged_predictions = 0
    inserted_snapshots = unchanged_snapshots = 0
    with connection.transaction():
        for run_record, prediction_record, snapshot_records in records:
            existing_run = _stored_run(connection, run_record)
            if existing_run is not None:
                _require_exact_row(
                    existing_run,
                    run_record.stable_columns(),
                    f"immutable Team Rating run conflict: {run_record.run_id}",
                )
                unchanged_runs += 1
            elif dry_run:
                inserted_runs += 1
            else:
                inserted_run = connection.execute(
                    """INSERT INTO team_rating_runs
                       (run_id, rating_version, artifact_version,
                        availability_mode, training_cutoff,
                        configuration_json, training_input_hash,
                        metrics_json, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT DO NOTHING RETURNING run_id""",
                    (run_record.run_id, *run_record.stable_columns(), created),
                ).fetchone()
                if inserted_run is not None:
                    inserted_runs += 1
                else:
                    _require_exact_row(
                        _stored_run(connection, run_record),
                        run_record.stable_columns(),
                        f"immutable Team Rating run conflict: {run_record.run_id}",
                    )
                    unchanged_runs += 1

            existing_prediction = _stored_prediction(connection, prediction_record)
            if existing_prediction is not None:
                _require_exact_row(
                    existing_prediction,
                    prediction_record.stable_columns(),
                    "immutable Team Rating prediction conflict: "
                    f"{prediction_record.run_id}/{prediction_record.match_id}",
                )
                unchanged_predictions += 1
            elif dry_run:
                inserted_predictions += 1
            else:
                inserted_prediction = connection.execute(
                    """INSERT INTO team_rating_predictions
                       (run_id, match_id, prediction_cutoff, cutoff_source,
                        radiant_team_id, dire_team_id, radiant_rating,
                        dire_rating, rating_diff, raw_probability,
                        radiant_roster_continuity, dire_roster_continuity,
                        support, input_hash, eventual_radiant_win, status,
                        created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT DO NOTHING RETURNING prediction_id""",
                    (
                        prediction_record.run_id,
                        *prediction_record.stable_columns(),
                        created,
                    ),
                ).fetchone()
                if inserted_prediction is not None:
                    inserted_predictions += 1
                else:
                    _require_exact_row(
                        _stored_prediction(connection, prediction_record),
                        prediction_record.stable_columns(),
                        "immutable Team Rating prediction conflict: "
                        f"{prediction_record.run_id}/{prediction_record.match_id}",
                    )
                    unchanged_predictions += 1

            if run_record.run_id not in requested_checkpoints:
                continue
            for snapshot_record in snapshot_records:
                existing_snapshot = _stored_snapshot(connection, snapshot_record)
                if existing_snapshot is not None:
                    _require_exact_row(
                        existing_snapshot,
                        snapshot_record.stable_columns(),
                        "immutable Team Rating state snapshot conflict: "
                        f"{snapshot_record.run_id}/"
                        f"{snapshot_record.as_of.isoformat()}/"
                        f"{snapshot_record.team_id}",
                    )
                    unchanged_snapshots += 1
                elif dry_run:
                    inserted_snapshots += 1
                else:
                    inserted_snapshot = connection.execute(
                        """INSERT INTO team_rating_state_snapshots
                           (snapshot_key, run_id, as_of, team_id, rating,
                            maps_seen, roster_json, last_observed_at,
                            state_hash, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT DO NOTHING RETURNING snapshot_key""",
                        (
                            snapshot_record.snapshot_key,
                            snapshot_record.run_id,
                            snapshot_record.as_of.isoformat(),
                            snapshot_record.team_id,
                            snapshot_record.rating,
                            snapshot_record.maps_seen,
                            snapshot_record.roster_json,
                            (
                                None
                                if snapshot_record.last_observed_at is None
                                else snapshot_record.last_observed_at.isoformat()
                            ),
                            snapshot_record.state_hash,
                            created,
                        ),
                    ).fetchone()
                    if inserted_snapshot is not None:
                        inserted_snapshots += 1
                    else:
                        _require_exact_row(
                            _stored_snapshot(connection, snapshot_record),
                            snapshot_record.stable_columns(),
                            "immutable Team Rating state snapshot conflict: "
                            f"{snapshot_record.run_id}/"
                            f"{snapshot_record.as_of.isoformat()}/"
                            f"{snapshot_record.team_id}",
                        )
                        unchanged_snapshots += 1

    return TeamRatingPersistenceCounts(
        inserted_runs=inserted_runs,
        unchanged_runs=unchanged_runs,
        inserted_predictions=inserted_predictions,
        unchanged_predictions=unchanged_predictions,
        inserted_snapshots=inserted_snapshots,
        unchanged_snapshots=unchanged_snapshots,
    )


__all__ = [
    "TeamRatingPersistenceCounts",
    "TeamRatingPredictionRecord",
    "TeamRatingRunRecord",
    "TeamRatingStateSnapshotRecord",
    "build_team_rating_storage_records",
    "persist_team_rating_runs",
]
