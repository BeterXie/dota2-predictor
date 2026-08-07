"""Cutoff-safe operational producer for prospective Team Rating P0."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from database.session import DatabaseRow, PostgresSession

from .raw_archive import canonical_json_bytes
from .team_rating import (
    TEAM_RATING_VERSION,
    RatingMapInput,
    TeamRatingConfig,
    TeamRatingState,
    canonical_rating_states,
    update_team_ratings,
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


def _rating_replay_order(value: AuthoritativeResult) -> tuple[datetime, datetime, int]:
    return value.row.started_at, value.row.completed_at, value.row.match_id


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


def canonical_team_rating_replay_results(
    values: Sequence[AuthoritativeResult],
) -> tuple[AuthoritativeResult, ...]:
    by_match: dict[int, AuthoritativeResult] = {}
    for value in values:
        if not isinstance(value, AuthoritativeResult):
            raise ValueError("results must contain AuthoritativeResult values")
        match_id = value.row.match_id
        existing = by_match.get(match_id)
        if existing is not None and existing != value:
            raise ValueError(f"conflicting result authority for match {match_id}")
        by_match[match_id] = value
    ordered = tuple(sorted(by_match.values(), key=_rating_replay_order))
    last_completed_by_team: dict[int, datetime] = {}
    for value in ordered:
        row = value.row
        for team_id in (row.radiant_team_id, row.dire_team_id):
            previous_completed = last_completed_by_team.get(team_id)
            if previous_completed is not None and row.started_at < previous_completed:
                raise ValueError("overlapping_team_match_chronology")
        last_completed_by_team[row.radiant_team_id] = row.completed_at
        last_completed_by_team[row.dire_team_id] = row.completed_at
    return ordered


def _replay_team_rating_results(
    values: Sequence[AuthoritativeResult],
    *,
    update_cutoff: datetime,
    config: TeamRatingConfig,
) -> tuple[tuple[AuthoritativeResult, ...], tuple[TeamRatingState, ...]]:
    cutoff = _utc(update_cutoff, "Team Rating replay cutoff")
    ordered = canonical_team_rating_replay_results(values)
    states: tuple[TeamRatingState, ...] = ()
    for result in ordered:
        usable_at = result.row.result_usable_at
        if usable_at is None or usable_at > cutoff:
            raise ValueError("result follows Team Rating replay cutoff")
        states = update_team_ratings(states, result.row, cutoff, config)
    return ordered, states


@dataclass(frozen=True)
class ProspectiveTeamRatingSeed:
    seed_hash: str
    config: TeamRatingConfig
    configuration_hash: str
    seed_as_of: datetime
    seed_training_cutoff: datetime
    source_manifest: tuple[AuthoritativeResult, ...]
    source_manifest_hash: str
    rating_replay_order: tuple[AuthoritativeResult, ...]
    rating_replay_order_hash: str
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
            self, "rating_replay_order", tuple(self.rating_replay_order)
        )
        object.__setattr__(
            self,
            "rating_replay_order_hash",
            _sha256(self.rating_replay_order_hash, "rating_replay_order_hash"),
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
    replay_order, states = _replay_team_rating_results(
        ordered,
        update_cutoff=cutoff,
        config=config,
    )
    configuration_json = _canonical_json(config.to_payload())
    source_json = _canonical_json([result.to_payload() for result in ordered])
    replay_json = _canonical_json(
        [result.to_payload() for result in replay_order]
    )
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
        "rating_replay_order": [result.to_payload() for result in replay_order],
        "rating_replay_order_hash": _hash_text(replay_json),
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
        rating_replay_order=replay_order,
        rating_replay_order_hash=_hash_text(replay_json),
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


def rebuild_prospective_team_rating_state(
    seed: ProspectiveTeamRatingSeed,
    applied_results: Sequence[AuthoritativeResult],
    *,
    cutoff: datetime,
    target_match_id: int | None,
) -> tuple[
    tuple[AuthoritativeResult, ...],
    tuple[AuthoritativeResult, ...],
    tuple[TeamRatingState, ...],
]:
    verify_prospective_team_rating_seed(seed)
    cutoff_at = _utc(cutoff, "prospective Team Rating cutoff")
    applied = canonical_authoritative_results(
        applied_results,
        after=seed.seed_as_of,
        cutoff=cutoff_at,
        target_match_id=target_match_id,
    )
    complete_authority = canonical_authoritative_results(
        (*seed.source_manifest, *applied),
        after=None,
        cutoff=cutoff_at,
        target_match_id=target_match_id,
    )
    replay_order, states = _replay_team_rating_results(
        complete_authority,
        update_cutoff=cutoff_at,
        config=seed.config,
    )
    return applied, replay_order, states


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
        rating_replay_order=tuple(
            _result_from_payload(value) for value in row["rating_replay_order"]
        ),
        rating_replay_order_hash=row["rating_replay_order_hash"],
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

    def load_base_state(
        self,
        seed: ProspectiveTeamRatingSeed,
        prediction_cutoff: datetime,
    ) -> BaseState:
        cutoff = _utc(prediction_cutoff, "prediction_cutoff")
        if seed.seed_as_of > cutoff:
            raise ValueError("seed state follows prospective cutoff")
        return BaseState(None, seed.seed_as_of, seed.state_hash, seed.states)

    def load_results(
        self,
        *,
        after: datetime,
        cutoff: datetime,
        observed_at: datetime,
        target_match_id: int | None,
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
        target_filter = "" if target_match_id is None else "AND result.match_id <> ?"
        parameters: tuple[object, ...] = (
            after_at.isoformat(),
            availability_cutoff.isoformat(),
        )
        if target_match_id is not None:
            parameters += (target_match_id,)
        rows = self.connection.execute(
            f"""SELECT result.match_id, status.series_id, status.event_id,
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
                   {target_filter}
                   AND result.radiant_win IS NOT NULL
                  AND result.duration > 0
                  AND result.radiant_team_id IS NOT NULL
                  AND result.dire_team_id IS NOT NULL
                  AND status.latest_raw_content_hash IS NOT NULL
                ORDER BY live_text_timestamp_utc(status.first_usable_at),
                         result.start_time, result.match_id""",
            parameters,
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

__all__ = [
    "AuthoritativeResult",
    "BaseState",
    "PROSPECTIVE_TEAM_RATING_ARTIFACT_VERSION",
    "PROSPECTIVE_TEAM_RATING_VERSION",
    "ProspectiveTeamRatingRepository",
    "ProspectiveTeamRatingSeed",
    "build_prospective_team_rating_seed",
    "canonical_authoritative_results",
    "canonical_team_rating_replay_results",
    "load_prospective_team_rating_seed_json",
    "rebuild_prospective_team_rating_state",
    "team_rating_state_hash",
    "verify_prospective_team_rating_seed",
]
