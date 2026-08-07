"""Cutoff-safe operational producer for prospective Team Rating P0."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from database.session import DatabaseRow, PostgresSession

from .prospective_rosh_shadow import TeamRatingAuthority
from .raw_archive import canonical_json_bytes
from .team_rating import (
    TEAM_RATING_VERSION,
    RatingMapInput,
    TeamRatingConfig,
    TeamRatingPrediction,
    TeamRatingState,
    TeamRatingTarget,
    canonical_rating_states,
    predict_team_rating,
    update_team_ratings,
)
from .team_rating_storage import (
    TeamRatingPersistenceCounts,
    TeamRatingPredictionRecord,
    TeamRatingRunRecord,
    TeamRatingStorageRecords,
    build_team_rating_state_snapshot_record,
    persist_team_rating_storage_records,
)


UTC = timezone.utc
PROSPECTIVE_TEAM_RATING_VERSION = "prospective-team-rating-v1"
PROSPECTIVE_TEAM_RATING_ARTIFACT_VERSION = (
    "prospective-team-rating-artifact-v1"
)
PROSPECTIVE_CUTOFF_SOURCE = "prospective_scheduled_start"
RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
)


def _canonical_json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_utc(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be a UTC timestamp") from error
    return _utc(parsed, field)


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: object, field: str) -> int | None:
    return None if value is None else _positive_int(value, field)


def _finite(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{field} must be finite")
    return float(value)


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _state_payload(states: Sequence[TeamRatingState]) -> list[dict[str, Any]]:
    return [state.to_payload() for state in canonical_rating_states(states)]


def team_rating_state_hash(states: Sequence[TeamRatingState]) -> str:
    return _hash_text(_canonical_json(_state_payload(states)))


@dataclass(frozen=True)
class AuthoritativeResult:
    row: RatingMapInput
    source_artifact_hash: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.row, RatingMapInput):
            raise ValueError("authoritative result row is invalid")
        if self.row.result_usable_at is None:
            raise ValueError("authoritative result must have result_usable_at")
        object.__setattr__(
            self,
            "source_artifact_hash",
            _sha256(self.source_artifact_hash, "source_artifact_hash"),
        )
        observed = _utc(self.observed_at, "observed_at")
        if self.row.result_usable_at > observed:
            raise ValueError("result was not available when observed")
        object.__setattr__(self, "observed_at", observed)

    def to_payload(self) -> dict[str, Any]:
        return {
            "result": self.row.to_payload(),
            "source_artifact_hash": self.source_artifact_hash,
            "observed_at": self.observed_at.isoformat(),
        }


def _result_order(value: AuthoritativeResult) -> tuple[datetime, datetime, int]:
    usable = value.row.result_usable_at
    if usable is None:
        raise AssertionError("validated result lacks result_usable_at")
    return usable, value.row.started_at, value.row.match_id


def canonical_authoritative_results(
    values: Sequence[AuthoritativeResult],
    *,
    after: datetime | None,
    cutoff: datetime,
    target_match_id: int | None = None,
) -> tuple[AuthoritativeResult, ...]:
    cutoff_at = _utc(cutoff, "result cutoff")
    after_at = None if after is None else _utc(after, "result lower bound")
    by_match: dict[int, AuthoritativeResult] = {}
    for value in values:
        if not isinstance(value, AuthoritativeResult):
            raise ValueError("results must contain AuthoritativeResult values")
        match_id = value.row.match_id
        if match_id == target_match_id:
            raise ValueError("target map cannot enter Team Rating state")
        usable_at = value.row.result_usable_at
        if usable_at is None or usable_at > cutoff_at:
            raise ValueError("result follows prospective prediction cutoff")
        if after_at is not None and usable_at <= after_at:
            raise ValueError("result is already represented by the base state")
        existing = by_match.get(match_id)
        if existing is not None and existing != value:
            raise ValueError(f"conflicting result authority for match {match_id}")
        by_match[match_id] = value
    return tuple(sorted(by_match.values(), key=_result_order))


@dataclass(frozen=True)
class ProspectiveTeamRatingSeed:
    seed_hash: str
    config: TeamRatingConfig
    configuration_hash: str
    seed_as_of: datetime
    seed_training_cutoff: datetime
    source_manifest: tuple[AuthoritativeResult, ...]
    source_manifest_hash: str
    states: tuple[TeamRatingState, ...]
    state_hash: str
    artifact_json: str
    artifact_hash: str
    frozen_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "seed_hash", _sha256(self.seed_hash, "seed_hash"))
        if not isinstance(self.config, TeamRatingConfig):
            raise ValueError("seed config is invalid")
        object.__setattr__(
            self,
            "configuration_hash",
            _sha256(self.configuration_hash, "configuration_hash"),
        )
        as_of = _utc(self.seed_as_of, "seed_as_of")
        cutoff = _utc(self.seed_training_cutoff, "seed_training_cutoff")
        frozen = _utc(self.frozen_at, "frozen_at")
        if cutoff > as_of or as_of > frozen:
            raise ValueError("seed timing is not causal")
        object.__setattr__(self, "seed_as_of", as_of)
        object.__setattr__(self, "seed_training_cutoff", cutoff)
        object.__setattr__(self, "frozen_at", frozen)
        object.__setattr__(self, "source_manifest", tuple(self.source_manifest))
        object.__setattr__(
            self,
            "source_manifest_hash",
            _sha256(self.source_manifest_hash, "source_manifest_hash"),
        )
        object.__setattr__(
            self, "states", canonical_rating_states(self.states)
        )
        object.__setattr__(self, "state_hash", _sha256(self.state_hash, "state_hash"))
        if not isinstance(self.artifact_json, str):
            raise ValueError("seed artifact_json is invalid")
        object.__setattr__(
            self, "artifact_hash", _sha256(self.artifact_hash, "artifact_hash")
        )
        if self.seed_hash != self.artifact_hash:
            raise ValueError("seed_hash must equal its content-addressed artifact hash")


def _build_prospective_team_rating_seed(
    *,
    config: TeamRatingConfig,
    source_results: Sequence[AuthoritativeResult],
    seed_as_of: datetime,
    seed_training_cutoff: datetime,
    frozen_at: datetime,
    verify: bool,
) -> ProspectiveTeamRatingSeed:
    as_of = _utc(seed_as_of, "seed_as_of")
    cutoff = _utc(seed_training_cutoff, "seed_training_cutoff")
    frozen = _utc(frozen_at, "frozen_at")
    if cutoff > as_of or as_of > frozen:
        raise ValueError("seed timing is not causal")
    ordered = canonical_authoritative_results(
        source_results,
        after=None,
        cutoff=cutoff,
    )
    states: tuple[TeamRatingState, ...] = ()
    for result in ordered:
        usable_at = result.row.result_usable_at
        if usable_at is None:
            raise AssertionError("validated seed result lacks availability")
        states = update_team_ratings(states, result.row, usable_at, config)
    configuration_json = _canonical_json(config.to_payload())
    source_json = _canonical_json([result.to_payload() for result in ordered])
    state_json = _canonical_json(_state_payload(states))
    artifact_payload = {
        "version": PROSPECTIVE_TEAM_RATING_VERSION,
        "rating_version": TEAM_RATING_VERSION,
        "configuration": config.to_payload(),
        "configuration_hash": _hash_text(configuration_json),
        "seed_as_of": as_of.isoformat(),
        "seed_training_cutoff": cutoff.isoformat(),
        "source_manifest": [result.to_payload() for result in ordered],
        "source_manifest_hash": _hash_text(source_json),
        "states": _state_payload(states),
        "state_hash": _hash_text(state_json),
        "frozen_at": frozen.isoformat(),
    }
    artifact_json = _canonical_json(artifact_payload)
    artifact_hash = _hash_text(artifact_json)
    seed = ProspectiveTeamRatingSeed(
        seed_hash=artifact_hash,
        config=config,
        configuration_hash=_hash_text(configuration_json),
        seed_as_of=as_of,
        seed_training_cutoff=cutoff,
        source_manifest=ordered,
        source_manifest_hash=_hash_text(source_json),
        states=states,
        state_hash=_hash_text(state_json),
        artifact_json=artifact_json,
        artifact_hash=artifact_hash,
        frozen_at=frozen,
    )
    if verify:
        verify_prospective_team_rating_seed(seed)
    return seed


def build_prospective_team_rating_seed(
    *,
    config: TeamRatingConfig,
    source_results: Sequence[AuthoritativeResult],
    seed_as_of: datetime,
    seed_training_cutoff: datetime,
    frozen_at: datetime,
) -> ProspectiveTeamRatingSeed:
    return _build_prospective_team_rating_seed(
        config=config,
        source_results=source_results,
        seed_as_of=seed_as_of,
        seed_training_cutoff=seed_training_cutoff,
        frozen_at=frozen_at,
        verify=True,
    )


def verify_prospective_team_rating_seed(seed: ProspectiveTeamRatingSeed) -> None:
    rebuilt = _build_prospective_team_rating_seed(
        config=seed.config,
        source_results=seed.source_manifest,
        seed_as_of=seed.seed_as_of,
        seed_training_cutoff=seed.seed_training_cutoff,
        frozen_at=seed.frozen_at,
        verify=False,
    )
    if rebuilt != seed:
        raise ValueError("prospective Team Rating seed exact replay disagrees")


@dataclass(frozen=True)
class ProspectiveTarget:
    target: TeamRatingTarget
    prediction_cutoff: datetime
    cutoff_source: str = PROSPECTIVE_CUTOFF_SOURCE

    def __post_init__(self) -> None:
        if not isinstance(self.target, TeamRatingTarget):
            raise ValueError("prospective target is invalid")
        if self.target.series_id is None:
            raise ValueError("prospective target series_id is unavailable")
        cutoff = _utc(self.prediction_cutoff, "prediction_cutoff")
        if cutoff > self.target.started_at:
            raise ValueError("prediction cutoff follows target start")
        if self.cutoff_source != PROSPECTIVE_CUTOFF_SOURCE:
            raise ValueError("prospective cutoff source is invalid")
        object.__setattr__(self, "prediction_cutoff", cutoff)

    def to_payload(self) -> dict[str, Any]:
        return {
            "target": self.target.to_payload(),
            "prediction_cutoff": self.prediction_cutoff.isoformat(),
            "cutoff_source": self.cutoff_source,
        }


@dataclass(frozen=True)
class ProspectiveTeamRatingRun:
    seed: ProspectiveTeamRatingSeed
    base_authority_hash: str | None
    base_as_of: datetime
    base_state_hash: str
    base_states: tuple[TeamRatingState, ...]
    applied_results: tuple[AuthoritativeResult, ...]
    state_before_target: tuple[TeamRatingState, ...]
    target: ProspectiveTarget
    prediction: TeamRatingPrediction
    training_input_hash: str
    artifact_json: str
    artifact_hash: str
    run_id: str
    created_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.seed, ProspectiveTeamRatingSeed):
            raise ValueError("run seed is invalid")
        if self.base_authority_hash is not None:
            object.__setattr__(
                self,
                "base_authority_hash",
                _sha256(self.base_authority_hash, "base_authority_hash"),
            )
        object.__setattr__(self, "base_as_of", _utc(self.base_as_of, "base_as_of"))
        object.__setattr__(
            self, "base_state_hash", _sha256(self.base_state_hash, "base_state_hash")
        )
        object.__setattr__(
            self, "base_states", canonical_rating_states(self.base_states)
        )
        object.__setattr__(self, "applied_results", tuple(self.applied_results))
        object.__setattr__(
            self,
            "state_before_target",
            canonical_rating_states(self.state_before_target),
        )
        if not isinstance(self.target, ProspectiveTarget):
            raise ValueError("run target is invalid")
        if not isinstance(self.prediction, TeamRatingPrediction):
            raise ValueError("run prediction is invalid")
        object.__setattr__(
            self,
            "training_input_hash",
            _sha256(self.training_input_hash, "training_input_hash"),
        )
        if not isinstance(self.artifact_json, str):
            raise ValueError("run artifact_json is invalid")
        object.__setattr__(
            self, "artifact_hash", _sha256(self.artifact_hash, "artifact_hash")
        )
        object.__setattr__(self, "run_id", _sha256(self.run_id, "run_id"))
        created = _utc(self.created_at, "created_at")
        available = _utc(self.available_at, "available_at")
        if created > available or available >= self.target.prediction_cutoff:
            raise ValueError("prospective prediction was not available before cutoff")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "available_at", available)


def _build_prospective_run(
    *,
    seed: ProspectiveTeamRatingSeed,
    base_authority_hash: str | None,
    base_as_of: datetime,
    base_states: Sequence[TeamRatingState],
    applied_results: Sequence[AuthoritativeResult],
    target: ProspectiveTarget,
    created_at: datetime,
    verify: bool,
) -> ProspectiveTeamRatingRun:
    verify_prospective_team_rating_seed(seed)
    created = _utc(created_at, "created_at")
    base_at = _utc(base_as_of, "base_as_of")
    if base_at > target.prediction_cutoff:
        raise ValueError("base state follows prospective cutoff")
    if base_authority_hash is None:
        if base_at != seed.seed_as_of or canonical_rating_states(base_states) != seed.states:
            raise ValueError("initial prospective state must equal the frozen seed")
    base = canonical_rating_states(base_states)
    base_hash = team_rating_state_hash(base)
    ordered = canonical_authoritative_results(
        applied_results,
        after=base_at,
        cutoff=target.prediction_cutoff,
        target_match_id=target.target.match_id,
    )
    states = base
    for result in ordered:
        usable_at = result.row.result_usable_at
        if usable_at is None:
            raise AssertionError("validated applied result lacks availability")
        states = update_team_ratings(states, result.row, usable_at, seed.config)
    prediction = predict_team_rating(
        states,
        target.target,
        target.prediction_cutoff,
        seed.config,
    )
    applied_json = _canonical_json([result.to_payload() for result in ordered])
    target_json = _canonical_json(target.to_payload())
    state_json = _canonical_json(_state_payload(states))
    configuration_json = _canonical_json(seed.config.to_payload())
    training_payload = {
        "version": PROSPECTIVE_TEAM_RATING_VERSION,
        "seed_hash": seed.seed_hash,
        "configuration_hash": seed.configuration_hash,
        "base_authority_hash": base_authority_hash,
        "base_as_of": base_at.isoformat(),
        "base_state_hash": base_hash,
        "applied_result_manifest_hash": _hash_text(applied_json),
        "state_before_hash": _hash_text(state_json),
        "target_manifest_hash": _hash_text(target_json),
        "prediction_cutoff": target.prediction_cutoff.isoformat(),
    }
    training_input_hash = _hash(training_payload)
    artifact_payload = {
        "version": PROSPECTIVE_TEAM_RATING_VERSION,
        "artifact_version": PROSPECTIVE_TEAM_RATING_ARTIFACT_VERSION,
        "rating_version": TEAM_RATING_VERSION,
        "availability_mode": "prospective",
        "seed_hash": seed.seed_hash,
        "configuration": seed.config.to_payload(),
        "configuration_hash": _hash_text(configuration_json),
        "base_authority_hash": base_authority_hash,
        "base_as_of": base_at.isoformat(),
        "base_states": _state_payload(base),
        "base_state_hash": base_hash,
        "applied_results": [result.to_payload() for result in ordered],
        "applied_result_manifest_hash": _hash_text(applied_json),
        "state_before_target": _state_payload(states),
        "state_before_hash": _hash_text(state_json),
        "target": target.to_payload(),
        "target_manifest_hash": _hash_text(target_json),
        "prediction": prediction.to_payload(),
        "training_input_hash": training_input_hash,
        "created_at": created.isoformat(),
        "available_at": created.isoformat(),
    }
    artifact_json = _canonical_json(artifact_payload)
    artifact_hash = _hash_text(artifact_json)
    run_id = _hash(
        {
            "schema": "prospective-team-rating-run/v1",
            "availability_mode": "prospective",
            "artifact_hash": artifact_hash,
        }
    )
    run = ProspectiveTeamRatingRun(
        seed=seed,
        base_authority_hash=base_authority_hash,
        base_as_of=base_at,
        base_state_hash=base_hash,
        base_states=base,
        applied_results=ordered,
        state_before_target=states,
        target=target,
        prediction=prediction,
        training_input_hash=training_input_hash,
        artifact_json=artifact_json,
        artifact_hash=artifact_hash,
        run_id=run_id,
        created_at=created,
        available_at=created,
    )
    if verify:
        verify_prospective_team_rating_run(run)
    return run


def build_prospective_team_rating_run(
    *,
    seed: ProspectiveTeamRatingSeed,
    base_authority_hash: str | None,
    base_as_of: datetime,
    base_states: Sequence[TeamRatingState],
    applied_results: Sequence[AuthoritativeResult],
    target: ProspectiveTarget,
    created_at: datetime,
) -> ProspectiveTeamRatingRun:
    return _build_prospective_run(
        seed=seed,
        base_authority_hash=base_authority_hash,
        base_as_of=base_as_of,
        base_states=base_states,
        applied_results=applied_results,
        target=target,
        created_at=created_at,
        verify=True,
    )


def verify_prospective_team_rating_run(run: ProspectiveTeamRatingRun) -> None:
    rebuilt = _build_prospective_run(
        seed=run.seed,
        base_authority_hash=run.base_authority_hash,
        base_as_of=run.base_as_of,
        base_states=run.base_states,
        applied_results=run.applied_results,
        target=run.target,
        created_at=run.created_at,
        verify=False,
    )
    if rebuilt != run:
        raise ValueError("prospective Team Rating exact replay disagrees")


def build_prospective_team_rating_storage_records(
    run: ProspectiveTeamRatingRun,
) -> TeamRatingStorageRecords:
    """Project a verified prospective run without relaxing reconstructed rules."""

    verify_prospective_team_rating_run(run)
    configuration_json = _canonical_json(
        {
            "artifact_hash": run.artifact_hash,
            "availability_mode": "prospective",
            "prospective_version": PROSPECTIVE_TEAM_RATING_VERSION,
            "seed_hash": run.seed.seed_hash,
            "configuration_hash": run.seed.configuration_hash,
            "config": run.seed.config.to_payload(),
            "base_authority_hash": run.base_authority_hash,
            "base_state_hash": run.base_state_hash,
            "applied_result_manifest_hash": _hash_text(
                _canonical_json(
                    [result.to_payload() for result in run.applied_results]
                )
            ),
            "target_manifest_hash": _hash_text(
                _canonical_json(run.target.to_payload())
            ),
        }
    )
    run_record = TeamRatingRunRecord(
        run_id=run.run_id,
        rating_version=TEAM_RATING_VERSION,
        artifact_version=PROSPECTIVE_TEAM_RATING_ARTIFACT_VERSION,
        availability_mode="prospective",
        training_cutoff=run.available_at,
        configuration_json=configuration_json,
        training_input_hash=run.training_input_hash,
        metrics_json=None,
        status="trained",
    )
    prediction = run.prediction
    target = run.target.target
    prediction_record = TeamRatingPredictionRecord(
        run_id=run.run_id,
        match_id=target.match_id,
        prediction_cutoff=run.target.prediction_cutoff,
        cutoff_source=run.target.cutoff_source,
        radiant_team_id=target.radiant_team_id,
        dire_team_id=target.dire_team_id,
        radiant_rating=prediction.radiant_rating,
        dire_rating=prediction.dire_rating,
        rating_diff=prediction.rating_diff,
        raw_probability=prediction.raw_probability,
        radiant_roster_continuity=prediction.radiant_roster_continuity,
        dire_roster_continuity=prediction.dire_roster_continuity,
        support=prediction.support,
        input_hash=prediction.input_hash,
        eventual_radiant_win=None,
        status="predicted",
    )
    snapshots = tuple(
        build_team_rating_state_snapshot_record(
            run.run_id,
            run.available_at,
            state,
        )
        for state in run.state_before_target
    )
    return run_record, prediction_record, snapshots


def _config_from_payload(value: object) -> TeamRatingConfig:
    row = _object(value, "configuration")
    return TeamRatingConfig(
        initial_rating=row["initial_rating"],
        scale=row["scale"],
        k_factor=row["k_factor"],
        inactivity_half_life_days=row["inactivity_half_life_days"],
        roster_carry_power=row["roster_carry_power"],
        radiant_side_logit=row["radiant_side_logit"],
        config_version=row["config_version"],
    )


def _state_from_payload(value: object) -> TeamRatingState:
    row = _object(value, "state")
    return TeamRatingState(
        team_id=row["team_id"],
        rating=row["rating"],
        maps_seen=row["maps_seen"],
        roster=tuple(row["roster"]),
        last_observed_at=(
            None
            if row["last_observed_at"] is None
            else _parse_utc(row["last_observed_at"], "last_observed_at")
        ),
    )


def _map_from_payload(value: object) -> RatingMapInput:
    row = _object(value, "result")
    return RatingMapInput(
        match_id=row["match_id"],
        series_id=row["series_id"],
        event_id=row["event_id"],
        started_at=_parse_utc(row["started_at"], "started_at"),
        completed_at=_parse_utc(row["completed_at"], "completed_at"),
        result_usable_at=_parse_utc(row["result_usable_at"], "result_usable_at"),
        radiant_team_id=row["radiant_team_id"],
        dire_team_id=row["dire_team_id"],
        radiant_roster=tuple(row["radiant_roster"]),
        dire_roster=tuple(row["dire_roster"]),
        radiant_win=row["radiant_win"],
    )


def _result_from_payload(value: object) -> AuthoritativeResult:
    row = _object(value, "result authority")
    return AuthoritativeResult(
        row=_map_from_payload(row["result"]),
        source_artifact_hash=row["source_artifact_hash"],
        observed_at=_parse_utc(row["observed_at"], "observed_at"),
    )


def load_prospective_team_rating_seed_json(
    artifact_json: str,
) -> ProspectiveTeamRatingSeed:
    try:
        payload = json.loads(artifact_json)
    except json.JSONDecodeError as error:
        raise ValueError("seed artifact is invalid JSON") from error
    if _canonical_json(payload) != artifact_json:
        raise ValueError("seed artifact must be canonical JSON")
    row = _object(payload, "seed artifact")
    seed = ProspectiveTeamRatingSeed(
        seed_hash=_hash_text(artifact_json),
        config=_config_from_payload(row["configuration"]),
        configuration_hash=row["configuration_hash"],
        seed_as_of=_parse_utc(row["seed_as_of"], "seed_as_of"),
        seed_training_cutoff=_parse_utc(
            row["seed_training_cutoff"], "seed_training_cutoff"
        ),
        source_manifest=tuple(
            _result_from_payload(value) for value in row["source_manifest"]
        ),
        source_manifest_hash=row["source_manifest_hash"],
        states=tuple(_state_from_payload(value) for value in row["states"]),
        state_hash=row["state_hash"],
        artifact_json=artifact_json,
        artifact_hash=_hash_text(artifact_json),
        frozen_at=_parse_utc(row["frozen_at"], "frozen_at"),
    )
    verify_prospective_team_rating_seed(seed)
    return seed


@dataclass(frozen=True)
class BaseState:
    authority_hash: str | None
    as_of: datetime
    state_hash: str
    states: tuple[TeamRatingState, ...]


@dataclass(frozen=True)
class ProductionResult:
    match_id: int
    status: str
    reason: str | None
    run_id: str | None = None
    artifact_hash: str | None = None
    prediction_id: int | None = None


@dataclass(frozen=True)
class ProductionReport:
    scanned: int
    produced: int
    unchanged: int
    failed: int
    results: tuple[ProductionResult, ...]


def _roster(rows: Sequence[DatabaseRow], radiant: bool) -> tuple[int, ...]:
    values = sorted(
        {
            int(row["account_id"])
            for row in rows
            if row["is_radiant"] is not None
            and bool(row["is_radiant"]) is radiant
            and row["account_id"] is not None
            and int(row["account_id"]) > 0
        }
    )
    return tuple(values) if len(values) == 5 else ()


class ProspectiveTeamRatingRepository:
    def __init__(self, connection: PostgresSession) -> None:
        self.connection = connection

    def store_seed(self, seed: ProspectiveTeamRatingSeed, *, dry_run: bool = False) -> bool:
        verify_prospective_team_rating_seed(seed)
        existing = self.connection.execute(
            """SELECT configuration_hash, seed_as_of, seed_training_cutoff,
                      source_manifest_json, source_manifest_hash, state_json,
                      state_hash, artifact_json, artifact_hash, frozen_at
                 FROM prospective_team_rating_seeds WHERE seed_hash=?""",
            (seed.seed_hash,),
        ).fetchone()
        source_json = _canonical_json(
            [result.to_payload() for result in seed.source_manifest]
        )
        state_json = _canonical_json(_state_payload(seed.states))
        expected = (
            seed.configuration_hash,
            seed.seed_as_of.isoformat(),
            seed.seed_training_cutoff.isoformat(),
            source_json,
            seed.source_manifest_hash,
            state_json,
            seed.state_hash,
            seed.artifact_json,
            seed.artifact_hash,
            seed.frozen_at.isoformat(),
        )
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError("immutable prospective Team Rating seed conflict")
            return False
        if dry_run:
            return True
        with self.connection.transaction():
            inserted = self.connection.execute(
                """INSERT INTO prospective_team_rating_seeds
                   (seed_hash, rating_version, configuration_json,
                    configuration_hash, seed_as_of, seed_training_cutoff,
                    source_manifest_json, source_manifest_hash, state_json,
                    state_hash, artifact_json, artifact_hash, frozen_at,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING RETURNING seed_hash""",
                (
                    seed.seed_hash,
                    TEAM_RATING_VERSION,
                    _canonical_json(seed.config.to_payload()),
                    *expected,
                    seed.frozen_at.isoformat(),
                ),
            ).fetchone()
            if inserted is None:
                raise ValueError("immutable prospective Team Rating seed conflict")
        return True

    def load_seed(self, prediction_cutoff: datetime) -> ProspectiveTeamRatingSeed | None:
        cutoff = _utc(prediction_cutoff, "prediction_cutoff")
        row = self.connection.execute(
            """SELECT artifact_json
                 FROM prospective_team_rating_seeds
                WHERE live_text_timestamp_utc(frozen_at) <
                          live_text_timestamp_utc(?)
                  AND live_text_timestamp_utc(seed_as_of) <=
                          live_text_timestamp_utc(?)
                ORDER BY live_text_timestamp_utc(frozen_at) DESC, seed_hash DESC
                LIMIT 1""",
            (cutoff.isoformat(), cutoff.isoformat()),
        ).fetchone()
        return None if row is None else load_prospective_team_rating_seed_json(row[0])

    def load_target(self, match_id: int) -> tuple[ProspectiveTarget, bool]:
        match = _positive_int(match_id, "match_id")
        row = self.connection.execute(
            """SELECT target.match_id, status.series_id, status.event_id,
                      target.start_time, target.radiant_team_id,
                      target.dire_team_id, target.radiant_win,
                      status.has_valid_result, status.first_usable_at
                 FROM matches AS target
                 JOIN match_ingest_status AS status
                   ON status.match_id=target.match_id
                 JOIN formal_events AS event ON event.event_id=status.event_id
                WHERE target.match_id=?
                  AND status.stage_in_scope=1
                  AND status.is_exhibition=0
                  AND status.is_forfeit=0
                  AND status.is_void_remake=0
                  AND (status.stage_scope='main_event' OR
                       (status.stage_scope='internal_lcq' AND
                        event.include_internal_lcq=1))""",
            (match,),
        ).fetchone()
        if row is None:
            raise ValueError("formal_target_unavailable")
        if row["start_time"] is None or int(row["start_time"]) <= 0:
            raise ValueError("scheduled_start_unavailable")
        radiant_team_id = _positive_int(row["radiant_team_id"], "radiant_team_id")
        dire_team_id = _positive_int(row["dire_team_id"], "dire_team_id")
        players = self.connection.execute(
            """SELECT account_id, is_radiant
                 FROM match_players WHERE match_id=?""",
            (match,),
        ).fetchall()
        started_at = datetime.fromtimestamp(int(row["start_time"]), UTC)
        target = ProspectiveTarget(
            target=TeamRatingTarget(
                match_id=match,
                series_id=_optional_positive_int(row["series_id"], "series_id"),
                event_id=str(row["event_id"]),
                started_at=started_at,
                radiant_team_id=radiant_team_id,
                dire_team_id=dire_team_id,
                radiant_roster=_roster(players, True),
                dire_roster=_roster(players, False),
            ),
            prediction_cutoff=started_at,
        )
        has_result = (
            row["radiant_win"] is not None
            or int(row["has_valid_result"]) != 0
            or row["first_usable_at"] is not None
        )
        return target, has_result

    def scan_target_ids(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        limit: int | None = None,
    ) -> tuple[int, ...]:
        start = _utc(start_at, "scan start")
        end = _utc(end_at, "scan end")
        if end < start:
            raise ValueError("scan end precedes start")
        limit_value = "" if limit is None else f" LIMIT {_positive_int(limit, 'limit')}"
        rows = self.connection.execute(
            """SELECT target.match_id
                 FROM matches AS target
                 JOIN match_ingest_status AS status
                   ON status.match_id=target.match_id
                 JOIN formal_events AS event ON event.event_id=status.event_id
                 LEFT JOIN prospective_team_rating_authorities AS authority
                   ON authority.match_id=target.match_id
                 LEFT JOIN LATERAL (
                     SELECT terminal, retry_at
                       FROM prospective_team_rating_attempts AS attempt_row
                      WHERE attempt_row.match_id=target.match_id
                      ORDER BY attempt_number DESC LIMIT 1
                 ) AS attempt ON true
                WHERE target.start_time >= ?
                  AND target.start_time <= ?
                  AND target.radiant_team_id IS NOT NULL
                  AND target.dire_team_id IS NOT NULL
                  AND status.stage_in_scope=1
                  AND status.is_exhibition=0
                  AND status.is_forfeit=0
                  AND status.is_void_remake=0
                  AND (status.stage_scope='main_event' OR
                       (status.stage_scope='internal_lcq' AND
                        event.include_internal_lcq=1))
                  AND authority.authority_hash IS NULL
                  AND (attempt.terminal IS NULL OR
                       (attempt.terminal=0 AND
                        live_text_timestamp_utc(attempt.retry_at)
                            <= live_text_timestamp_utc(?)))
                ORDER BY target.start_time, target.match_id"""
            + limit_value,
            (int(start.timestamp()), int(end.timestamp()), start.isoformat()),
        ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def load_base_state(
        self,
        seed: ProspectiveTeamRatingSeed,
        prediction_cutoff: datetime,
    ) -> BaseState:
        cutoff = _utc(prediction_cutoff, "prediction_cutoff")
        row = self.connection.execute(
            """SELECT authority_hash, available_at,
                      state_before_json, state_before_hash
                 FROM prospective_team_rating_authorities
                WHERE seed_hash=?
                  AND configuration_hash=?
                  AND live_text_timestamp_utc(prediction_cutoff)
                      <= live_text_timestamp_utc(?)
                ORDER BY live_text_timestamp_utc(prediction_cutoff) DESC,
                         match_id DESC
                LIMIT 1""",
            (seed.seed_hash, seed.configuration_hash, cutoff.isoformat()),
        ).fetchone()
        if row is None:
            return BaseState(None, seed.seed_as_of, seed.state_hash, seed.states)
        state_json = str(row["state_before_json"])
        if _hash_text(state_json) != row["state_before_hash"]:
            raise ValueError("prospective Team Rating state hash disagrees")
        values = json.loads(state_json)
        states = tuple(_state_from_payload(value) for value in values)
        if team_rating_state_hash(states) != row["state_before_hash"]:
            raise ValueError("prospective Team Rating state replay disagrees")
        return BaseState(
            str(row["authority_hash"]),
            _parse_utc(row["available_at"], "base available_at"),
            str(row["state_before_hash"]),
            states,
        )

    def load_results(
        self,
        *,
        after: datetime,
        cutoff: datetime,
        observed_at: datetime,
        target_match_id: int,
        allow_seed_observation: bool = False,
    ) -> tuple[AuthoritativeResult, ...]:
        after_at = _utc(after, "result lower bound")
        cutoff_at = _utc(cutoff, "result cutoff")
        observed = _utc(observed_at, "observed_at")
        if observed >= cutoff_at and not allow_seed_observation:
            raise ValueError("result scan occurred after prediction cutoff")
        availability_cutoff = (
            cutoff_at if allow_seed_observation else min(cutoff_at, observed)
        )
        rows = self.connection.execute(
            """SELECT result.match_id, status.series_id, status.event_id,
                      result.start_time, result.duration,
                      result.radiant_team_id, result.dire_team_id,
                      result.radiant_win, status.first_usable_at,
                      status.latest_raw_content_hash
                 FROM formal_map_eligibility AS eligible
                 JOIN matches AS result ON result.match_id=eligible.match_id
                 JOIN match_ingest_status AS status
                   ON status.match_id=result.match_id
                WHERE live_text_timestamp_utc(status.first_usable_at)
                          > live_text_timestamp_utc(?)
                  AND live_text_timestamp_utc(status.first_usable_at)
                          <= live_text_timestamp_utc(?)
                  AND result.match_id <> ?
                  AND result.radiant_win IS NOT NULL
                  AND result.duration > 0
                  AND result.radiant_team_id IS NOT NULL
                  AND result.dire_team_id IS NOT NULL
                  AND status.latest_raw_content_hash IS NOT NULL
                ORDER BY live_text_timestamp_utc(status.first_usable_at),
                         result.start_time, result.match_id""",
            (
                after_at.isoformat(),
                availability_cutoff.isoformat(),
                target_match_id,
            ),
        ).fetchall()
        player_rows = self.connection.execute(
            """SELECT player.match_id, player.account_id, player.is_radiant
                 FROM match_players AS player
                 JOIN match_ingest_status AS status
                   ON status.match_id=player.match_id
                WHERE live_text_timestamp_utc(status.first_usable_at)
                          > live_text_timestamp_utc(?)
                  AND live_text_timestamp_utc(status.first_usable_at)
                          <= live_text_timestamp_utc(?)""",
            (after_at.isoformat(), availability_cutoff.isoformat()),
        ).fetchall()
        by_match: dict[int, list[DatabaseRow]] = {}
        for player in player_rows:
            by_match.setdefault(int(player["match_id"]), []).append(player)
        results = []
        for row in rows:
            match_id = int(row["match_id"])
            started_at = datetime.fromtimestamp(int(row["start_time"]), UTC)
            results.append(
                AuthoritativeResult(
                    row=RatingMapInput(
                        match_id=match_id,
                        series_id=_optional_positive_int(row["series_id"], "series_id"),
                        event_id=str(row["event_id"]),
                        started_at=started_at,
                        completed_at=started_at + timedelta(seconds=int(row["duration"])),
                        result_usable_at=_parse_utc(
                            row["first_usable_at"], "result_usable_at"
                        ),
                        radiant_team_id=int(row["radiant_team_id"]),
                        dire_team_id=int(row["dire_team_id"]),
                        radiant_roster=_roster(by_match.get(match_id, ()), True),
                        dire_roster=_roster(by_match.get(match_id, ()), False),
                        radiant_win=bool(row["radiant_win"]),
                    ),
                    source_artifact_hash=str(row["latest_raw_content_hash"]),
                    observed_at=observed,
                )
            )
        return canonical_authoritative_results(
            results,
            after=after_at,
            cutoff=cutoff_at,
            target_match_id=target_match_id,
        )

    def load_seed_results(
        self,
        *,
        training_cutoff: datetime,
        observed_at: datetime,
    ) -> tuple[AuthoritativeResult, ...]:
        return self.load_results(
            after=datetime(2000, 1, 1, tzinfo=UTC),
            cutoff=training_cutoff,
            observed_at=observed_at,
            target_match_id=0,
            allow_seed_observation=True,
        )

    def existing_authority(self, match_id: int) -> DatabaseRow | None:
        return self.connection.execute(
            """SELECT authority_hash, run_id, prediction_id, artifact_hash
                 FROM prospective_team_rating_authorities
                WHERE match_id=?""",
            (_positive_int(match_id, "match_id"),),
        ).fetchone()

    def persist_run(
        self,
        run: ProspectiveTeamRatingRun,
        *,
        dry_run: bool = False,
    ) -> tuple[TeamRatingPersistenceCounts, int | None, bool]:
        verify_prospective_team_rating_run(run)
        records = build_prospective_team_rating_storage_records(run)
        existing = self.existing_authority(run.target.target.match_id)
        if existing is not None:
            if (
                existing["authority_hash"] != run.artifact_hash
                or existing["run_id"] != run.run_id
                or existing["artifact_hash"] != run.artifact_hash
            ):
                raise ValueError("immutable prospective Team Rating authority conflict")
            return TeamRatingPersistenceCounts(unchanged_runs=1, unchanged_predictions=1), int(existing["prediction_id"]), False
        if dry_run:
            counts = persist_team_rating_storage_records(
                self.connection,
                (records,),
                dry_run=True,
                checkpoint_run_ids=(run.run_id,),
                created_at=run.created_at,
            )
            return counts, None, True
        applied_json = _canonical_json(
            [result.to_payload() for result in run.applied_results]
        )
        state_json = _canonical_json(_state_payload(run.state_before_target))
        target_json = _canonical_json(run.target.to_payload())
        with self.connection.transaction():
            counts = persist_team_rating_storage_records(
                self.connection,
                (records,),
                checkpoint_run_ids=(run.run_id,),
                created_at=run.created_at,
            )
            prediction = self.connection.execute(
                """SELECT prediction_id FROM team_rating_predictions
                    WHERE run_id=? AND match_id=?""",
                (run.run_id, run.target.target.match_id),
            ).fetchone()
            if prediction is None:
                raise RuntimeError("prospective Team Rating prediction insert is unavailable")
            prediction_id = int(prediction[0])
            inserted = self.connection.execute(
                """INSERT INTO prospective_team_rating_authorities
                   (authority_hash, run_id, prediction_id, match_id, series_id,
                    prediction_cutoff, seed_hash, configuration_hash,
                    base_authority_hash, base_as_of, base_state_hash,
                    applied_result_manifest_json,
                    applied_result_manifest_hash, state_before_json,
                    state_before_hash, target_manifest_json,
                    target_manifest_hash, training_input_hash, artifact_json,
                    artifact_hash, available_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING RETURNING authority_hash""",
                (
                    run.artifact_hash,
                    run.run_id,
                    prediction_id,
                    run.target.target.match_id,
                    run.target.target.series_id,
                    run.target.prediction_cutoff.isoformat(),
                    run.seed.seed_hash,
                    run.seed.configuration_hash,
                    run.base_authority_hash,
                    run.base_as_of.isoformat(),
                    run.base_state_hash,
                    applied_json,
                    _hash_text(applied_json),
                    state_json,
                    _hash_text(state_json),
                    target_json,
                    _hash_text(target_json),
                    run.training_input_hash,
                    run.artifact_json,
                    run.artifact_hash,
                    run.available_at.isoformat(),
                    run.created_at.isoformat(),
                ),
            ).fetchone()
            if inserted is None:
                raise ValueError("immutable prospective Team Rating authority conflict")
        return counts, prediction_id, True

    def record_attempt(
        self,
        *,
        match_id: int,
        prediction_cutoff: datetime,
        attempted_at: datetime,
        reason: str,
        terminal: bool,
    ) -> None:
        match = _positive_int(match_id, "match_id")
        cutoff = _utc(prediction_cutoff, "prediction_cutoff")
        attempted = _utc(attempted_at, "attempted_at")
        if not reason or len(reason) > 200:
            raise ValueError("attempt reason is invalid")
        existing_terminal = self.connection.execute(
            """SELECT 1 FROM prospective_team_rating_attempts
                WHERE match_id=? AND terminal=1 LIMIT 1""",
            (match,),
        ).fetchone()
        if existing_terminal is not None:
            return
        prior = int(
            self.connection.execute(
                """SELECT COUNT(*) FROM prospective_team_rating_attempts
                    WHERE match_id=?""",
                (match,),
            ).scalar_one()
        )
        attempt_number = prior + 1
        retry_at = None
        if not terminal and attempt_number <= len(RETRY_DELAYS):
            candidate = attempted + RETRY_DELAYS[attempt_number - 1]
            retry_at = min(candidate, cutoff)
            terminal = retry_at >= cutoff
        elif not terminal:
            terminal = True
        payload = {
            "version": PROSPECTIVE_TEAM_RATING_VERSION,
            "match_id": match,
            "prediction_cutoff": cutoff.isoformat(),
            "attempt_number": attempt_number,
            "attempted_at": attempted.isoformat(),
            "reason": reason,
            "retry_at": None if retry_at is None else retry_at.isoformat(),
            "terminal": terminal,
        }
        attempt_hash = _hash(payload)
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO prospective_team_rating_attempts
                   (attempt_hash, match_id, prediction_cutoff, attempt_number,
                    attempted_at, reason, retry_at, terminal, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                (
                    attempt_hash,
                    match,
                    cutoff.isoformat(),
                    attempt_number,
                    attempted.isoformat(),
                    reason,
                    None if retry_at is None else retry_at.isoformat(),
                    int(terminal),
                    attempted.isoformat(),
                ),
            )

    def load_rosh_team_rating_authority(
        self, match_id: int
    ) -> TeamRatingAuthority | None:
        row = self.connection.execute(
            """SELECT prediction.prediction_id, prediction.run_id,
                      prediction.match_id, prediction.prediction_cutoff,
                      prediction.raw_probability, prediction.input_hash,
                      run.training_input_hash, run.rating_version,
                      run.artifact_version, authority.artifact_hash
                 FROM prospective_team_rating_authorities AS authority
                 JOIN team_rating_predictions AS prediction
                   ON prediction.prediction_id=authority.prediction_id
                 JOIN team_rating_runs AS run ON run.run_id=prediction.run_id
                WHERE authority.match_id=?
                  AND run.availability_mode='prospective'
                  AND run.status='trained'
                  AND prediction.status='predicted'
                  AND prediction.eventual_radiant_win IS NULL""",
            (_positive_int(match_id, "match_id"),),
        ).fetchone()
        if row is None:
            return None
        return TeamRatingAuthority(
            prediction_id=int(row["prediction_id"]),
            run_id=str(row["run_id"]),
            prediction_cutoff=_parse_utc(
                row["prediction_cutoff"], "prediction_cutoff"
            ),
            probability=float(row["raw_probability"]),
            input_hash=str(row["input_hash"]),
            training_input_hash=str(row["training_input_hash"]),
            rating_version=str(row["rating_version"]),
            artifact_version=str(row["artifact_version"]),
            artifact_hash=str(row["artifact_hash"]),
        )

    def resolve_rosh_team_rating_authority(
        self,
        match_id: int,
        *,
        observed_at: datetime,
    ) -> TeamRatingAuthority | None:
        """Return P0 or append the stable shadow dependency failure."""

        authority = self.load_rosh_team_rating_authority(match_id)
        if authority is not None:
            return authority
        target, _has_result = self.load_target(match_id)
        observed = _utc(observed_at, "observed_at")
        payload = {
            "version": PROSPECTIVE_TEAM_RATING_VERSION,
            "match_id": match_id,
            "prediction_cutoff": target.prediction_cutoff.isoformat(),
            "missing_reason": "prospective_team_rating_unavailable",
        }
        failure_hash = _hash(payload)
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO prospective_rosh_team_rating_failures
                   (failure_hash, match_id, prediction_cutoff, missing_reason,
                    observed_at, created_at)
                   VALUES (?, ?, ?, 'prospective_team_rating_unavailable', ?, ?)
                   ON CONFLICT DO NOTHING""",
                (
                    failure_hash,
                    match_id,
                    target.prediction_cutoff.isoformat(),
                    observed.isoformat(),
                    observed.isoformat(),
                ),
            )
        return None

    def settle(self, match_id: int, *, settled_at: datetime) -> bool:
        settled = _utc(settled_at, "settled_at")
        row = self.connection.execute(
            """SELECT authority.authority_hash, authority.prediction_cutoff,
                      target.radiant_win, target.start_time, target.duration,
                      status.has_valid_result, status.basic_result_state,
                      status.first_usable_at, status.latest_raw_content_hash
                 FROM prospective_team_rating_authorities AS authority
                 JOIN matches AS target ON target.match_id=authority.match_id
                 JOIN match_ingest_status AS status
                   ON status.match_id=authority.match_id
                WHERE authority.match_id=?""",
            (_positive_int(match_id, "match_id"),),
        ).fetchone()
        if row is None:
            raise ValueError("prospective_team_rating_unavailable")
        if (
            row["radiant_win"] is None
            or int(row["has_valid_result"]) != 1
            or row["basic_result_state"] != "ready"
            or row["first_usable_at"] is None
            or row["latest_raw_content_hash"] is None
        ):
            raise ValueError("authoritative_settlement_unavailable")
        payload = {
            "version": PROSPECTIVE_TEAM_RATING_VERSION,
            "authority_hash": row["authority_hash"],
            "eventual_radiant_win": bool(row["radiant_win"]),
            "result_artifact_hash": row["latest_raw_content_hash"],
            "result_usable_at": _parse_utc(
                row["first_usable_at"], "result_usable_at"
            ).isoformat(),
            "settled_at": settled.isoformat(),
        }
        settlement_hash = _hash(payload)
        with self.connection.transaction():
            inserted = self.connection.execute(
                """INSERT INTO prospective_team_rating_settlements
                   (settlement_hash, authority_hash, eventual_radiant_win,
                    result_artifact_hash, result_usable_at, settled_at,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING RETURNING settlement_hash""",
                (
                    settlement_hash,
                    row["authority_hash"],
                    int(bool(row["radiant_win"])),
                    row["latest_raw_content_hash"],
                    payload["result_usable_at"],
                    settled.isoformat(),
                    settled.isoformat(),
                ),
            ).fetchone()
        return inserted is not None


def archive_run_artifact(run: ProspectiveTeamRatingRun, root: Path) -> Path:
    verify_prospective_team_rating_run(run)
    path = root / "prospective-team-rating" / f"{run.artifact_hash}.json"
    if path.exists():
        if path.read_text(encoding="utf-8") != run.artifact_json:
            raise ValueError("immutable prospective Team Rating artifact conflict")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(run.artifact_json, encoding="utf-8")
    return path


def produce_match(
    repository: ProspectiveTeamRatingRepository,
    match_id: int,
    *,
    now: datetime,
    dry_run: bool = False,
    artifact_root: Path | None = None,
) -> ProductionResult:
    observed = _utc(now, "now")
    existing = repository.existing_authority(match_id)
    if existing is not None:
        return ProductionResult(
            match_id,
            "unchanged",
            None,
            str(existing["run_id"]),
            str(existing["artifact_hash"]),
            int(existing["prediction_id"]),
        )
    try:
        target, has_result = repository.load_target(match_id)
    except ValueError as error:
        return ProductionResult(match_id, "failed", str(error))
    if observed >= target.prediction_cutoff:
        if not dry_run:
            repository.record_attempt(
                match_id=match_id,
                prediction_cutoff=target.prediction_cutoff,
                attempted_at=observed,
                reason="prediction_cutoff_passed",
                terminal=True,
            )
        return ProductionResult(match_id, "failed", "prediction_cutoff_passed")
    if has_result:
        if not dry_run:
            repository.record_attempt(
                match_id=match_id,
                prediction_cutoff=target.prediction_cutoff,
                attempted_at=observed,
                reason="target_result_already_available",
                terminal=True,
            )
        return ProductionResult(
            match_id, "failed", "target_result_already_available"
        )
    seed = repository.load_seed(target.prediction_cutoff)
    if seed is None:
        if not dry_run:
            repository.record_attempt(
                match_id=match_id,
                prediction_cutoff=target.prediction_cutoff,
                attempted_at=observed,
                reason="prospective_team_rating_seed_unavailable",
                terminal=False,
            )
        return ProductionResult(
            match_id, "failed", "prospective_team_rating_seed_unavailable"
        )
    base = repository.load_base_state(seed, target.prediction_cutoff)
    results = repository.load_results(
        after=base.as_of,
        cutoff=target.prediction_cutoff,
        observed_at=observed,
        target_match_id=match_id,
    )
    run = build_prospective_team_rating_run(
        seed=seed,
        base_authority_hash=base.authority_hash,
        base_as_of=base.as_of,
        base_states=base.states,
        applied_results=results,
        target=target,
        created_at=observed,
    )
    _counts, prediction_id, inserted = repository.persist_run(run, dry_run=dry_run)
    if artifact_root is not None and not dry_run:
        archive_run_artifact(run, artifact_root)
    return ProductionResult(
        match_id=match_id,
        status="produced" if inserted else "unchanged",
        reason=None,
        run_id=run.run_id,
        artifact_hash=run.artifact_hash,
        prediction_id=prediction_id,
    )


def run_producer_once(
    repository: ProspectiveTeamRatingRepository,
    *,
    now: datetime,
    match_id: int | None = None,
    scan_start: datetime | None = None,
    scan_end: datetime | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    artifact_root: Path | None = None,
) -> ProductionReport:
    observed = _utc(now, "now")
    if match_id is None:
        start = (
            observed - timedelta(hours=24)
            if scan_start is None
            else _utc(scan_start, "scan_start")
        )
        end = (
            observed + timedelta(hours=24)
            if scan_end is None
            else _utc(scan_end, "scan_end")
        )
        match_ids = repository.scan_target_ids(
            start_at=start,
            end_at=end,
            limit=limit,
        )
    else:
        match_ids = (_positive_int(match_id, "match_id"),)
    produced_results = []
    for value in match_ids:
        try:
            result = produce_match(
                repository,
                value,
                now=observed,
                dry_run=dry_run,
                artifact_root=artifact_root,
            )
        except Exception as error:
            reason = f"producer_error_{type(error).__name__}"[:200]
            if not dry_run:
                try:
                    target, _has_result = repository.load_target(value)
                    repository.record_attempt(
                        match_id=value,
                        prediction_cutoff=target.prediction_cutoff,
                        attempted_at=observed,
                        reason=reason,
                        terminal=False,
                    )
                except Exception:
                    pass
            result = ProductionResult(value, "failed", reason)
        produced_results.append(result)
    results = tuple(produced_results)
    return ProductionReport(
        scanned=len(results),
        produced=sum(result.status == "produced" for result in results),
        unchanged=sum(result.status == "unchanged" for result in results),
        failed=sum(result.status == "failed" for result in results),
        results=results,
    )


__all__ = [
    "AuthoritativeResult",
    "BaseState",
    "PROSPECTIVE_CUTOFF_SOURCE",
    "PROSPECTIVE_TEAM_RATING_ARTIFACT_VERSION",
    "PROSPECTIVE_TEAM_RATING_VERSION",
    "ProductionReport",
    "ProductionResult",
    "ProspectiveTarget",
    "ProspectiveTeamRatingRepository",
    "ProspectiveTeamRatingRun",
    "ProspectiveTeamRatingSeed",
    "archive_run_artifact",
    "build_prospective_team_rating_run",
    "build_prospective_team_rating_seed",
    "build_prospective_team_rating_storage_records",
    "canonical_authoritative_results",
    "load_prospective_team_rating_seed_json",
    "produce_match",
    "run_producer_once",
    "team_rating_state_hash",
    "verify_prospective_team_rating_run",
    "verify_prospective_team_rating_seed",
]
