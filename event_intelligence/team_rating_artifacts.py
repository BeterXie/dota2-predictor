"""Replayable, self-verifying artifacts for Team Rating predictions."""

from __future__ import annotations

import hashlib
import hmac
import json
import platform
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .raw_archive import canonical_json_bytes
from .team_rating import (
    TEAM_RATING_VERSION,
    RatingMapInput,
    TeamRatingConfig,
    TeamRatingPrediction,
    TeamRatingState,
    TeamRatingTarget,
    canonical_rating_states,
    canonical_training_corpus,
    predict_team_rating,
    rating_training_input_hash,
    replay_team_ratings,
)


TEAM_RATING_ARTIFACT_VERSION = "team-rating-artifact-v1"
TEAM_RATING_ARTIFACT_SCHEMA = "team-rating-artifact/v1"
TEAM_RATING_RUNTIME_VERSIONS = (
    ("python_implementation", platform.python_implementation()),
    ("python_version", platform.python_version()),
)

_ARTIFACT_FIELDS = frozenset(
    {
        "schema",
        "artifact_version",
        "rating_version",
        "config",
        "training_cutoff",
        "ordered_training_corpus",
        "training_input_hash",
        "state_before_target",
        "target",
        "prediction",
        "runtime_versions",
        "artifact_hash",
    }
)
_CONFIG_FIELDS = frozenset(
    {
        "initial_rating",
        "scale",
        "k_factor",
        "inactivity_half_life_days",
        "roster_carry_power",
        "radiant_side_logit",
        "config_version",
    }
)
_MAP_FIELDS = frozenset(
    {
        "match_id",
        "series_id",
        "event_id",
        "started_at",
        "completed_at",
        "result_usable_at",
        "radiant_team_id",
        "dire_team_id",
        "radiant_roster",
        "dire_roster",
        "radiant_win",
    }
)
_STATE_FIELDS = frozenset(
    {"team_id", "rating", "maps_seen", "roster", "last_observed_at"}
)
_TARGET_FIELDS = frozenset(
    {
        "match_id",
        "series_id",
        "event_id",
        "started_at",
        "radiant_team_id",
        "dire_team_id",
        "radiant_roster",
        "dire_roster",
    }
)
_PREDICTION_FIELDS = frozenset(
    {
        "match_id",
        "prediction_cutoff",
        "radiant_rating",
        "dire_rating",
        "rating_diff",
        "radiant_side_logit",
        "raw_probability",
        "radiant_roster_continuity",
        "dire_roster_continuity",
        "support",
        "input_hash",
    }
)


def _hash(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _utc(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    return _utc(parsed, field)


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


def _array(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer or null")
    return value


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _strict_json_object(payload_json: str) -> Mapping[str, Any]:
    if not isinstance(payload_json, str):
        raise ValueError("Team Rating artifact JSON must be a string")

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
        raise ValueError("Team Rating artifact JSON is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("Team Rating artifact must be an object")
    return payload


@dataclass(frozen=True)
class TeamRatingArtifact:
    artifact_version: str
    rating_version: str
    config: TeamRatingConfig
    training_cutoff: datetime
    ordered_training_corpus: tuple[RatingMapInput, ...]
    training_input_hash: str
    state_before_target: tuple[TeamRatingState, ...]
    target: TeamRatingTarget
    prediction: TeamRatingPrediction
    runtime_versions: tuple[tuple[str, str], ...]
    artifact_hash: str

    def to_payload(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": TEAM_RATING_ARTIFACT_SCHEMA,
            "artifact_version": self.artifact_version,
            "rating_version": self.rating_version,
            "config": self.config.to_payload(),
            "training_cutoff": self.training_cutoff.isoformat(),
            "ordered_training_corpus": [
                row.to_payload() for row in self.ordered_training_corpus
            ],
            "training_input_hash": self.training_input_hash,
            "state_before_target": [
                state.to_payload() for state in self.state_before_target
            ],
            "target": self.target.to_payload(),
            "prediction": self.prediction.to_payload(),
            "runtime_versions": dict(self.runtime_versions),
        }
        if include_hash:
            payload["artifact_hash"] = self.artifact_hash
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())


def build_team_rating_artifact(
    rows: Iterable[RatingMapInput],
    *,
    target: RatingMapInput | TeamRatingTarget,
    prediction_cutoff: datetime,
    training_cutoff: datetime,
    config: TeamRatingConfig,
) -> TeamRatingArtifact:
    if not isinstance(config, TeamRatingConfig):
        raise ValueError("config must be a TeamRatingConfig")
    target_value = (
        TeamRatingTarget.from_map(target)
        if isinstance(target, RatingMapInput)
        else target
    )
    if not isinstance(target_value, TeamRatingTarget):
        raise ValueError("target must be a RatingMapInput or TeamRatingTarget")
    prediction_at = _utc(prediction_cutoff, "prediction_cutoff")
    training_at = _utc(training_cutoff, "training_cutoff")
    if training_at > prediction_at:
        raise ValueError("training_cutoff cannot follow prediction_cutoff")
    if prediction_at > target_value.started_at:
        raise ValueError("prediction_cutoff cannot follow target started_at")
    corpus = canonical_training_corpus(rows, training_at)
    if any(row.match_id == target_value.match_id for row in corpus):
        raise ValueError("target match cannot enter its Team Rating training corpus")
    states = replay_team_ratings(corpus, training_at, config)
    prediction = predict_team_rating(
        states,
        target_value,
        prediction_at,
        config,
    )
    artifact = TeamRatingArtifact(
        artifact_version=TEAM_RATING_ARTIFACT_VERSION,
        rating_version=TEAM_RATING_VERSION,
        config=config,
        training_cutoff=training_at,
        ordered_training_corpus=corpus,
        training_input_hash=rating_training_input_hash(
            corpus,
            training_at,
            config,
        ),
        state_before_target=states,
        target=target_value,
        prediction=prediction,
        runtime_versions=TEAM_RATING_RUNTIME_VERSIONS,
        artifact_hash="",
    )
    return replace(
        artifact,
        artifact_hash=_hash(artifact.to_payload(include_hash=False)),
    )


def verify_team_rating_artifact(artifact: TeamRatingArtifact) -> None:
    if not isinstance(artifact, TeamRatingArtifact):
        raise ValueError("artifact must be a TeamRatingArtifact")
    if artifact.artifact_version != TEAM_RATING_ARTIFACT_VERSION:
        raise ValueError("unsupported Team Rating artifact version")
    if artifact.rating_version != TEAM_RATING_VERSION:
        raise ValueError("unsupported Team Rating version")
    if artifact.runtime_versions != TEAM_RATING_RUNTIME_VERSIONS:
        raise ValueError("Team Rating runtime identity does not match")
    training_at = _utc(artifact.training_cutoff, "training_cutoff")
    if training_at > artifact.prediction.prediction_cutoff:
        raise ValueError("training_cutoff cannot follow prediction_cutoff")
    if artifact.prediction.prediction_cutoff > artifact.target.started_at:
        raise ValueError("prediction_cutoff cannot follow target started_at")
    corpus = canonical_training_corpus(artifact.ordered_training_corpus, training_at)
    if corpus != artifact.ordered_training_corpus:
        raise ValueError("Team Rating training corpus is not canonical")
    if any(row.match_id == artifact.target.match_id for row in corpus):
        raise ValueError("target match entered the Team Rating training corpus")
    expected_input_hash = rating_training_input_hash(
        corpus,
        training_at,
        artifact.config,
    )
    claimed_input_hash = _digest(
        artifact.training_input_hash,
        "training_input_hash",
    )
    if not hmac.compare_digest(claimed_input_hash, expected_input_hash):
        raise ValueError("Team Rating training input hash does not recompute")
    expected_states = replay_team_ratings(corpus, training_at, artifact.config)
    actual_states = canonical_rating_states(artifact.state_before_target)
    if canonical_json_bytes(
        [state.to_payload() for state in actual_states]
    ) != canonical_json_bytes([state.to_payload() for state in expected_states]):
        raise ValueError("Team Rating state does not replay from its corpus")
    expected_prediction = predict_team_rating(
        expected_states,
        artifact.target,
        artifact.prediction.prediction_cutoff,
        artifact.config,
    )
    if canonical_json_bytes(artifact.prediction.to_payload()) != canonical_json_bytes(
        expected_prediction.to_payload()
    ):
        raise ValueError("Team Rating prediction does not replay from its state")
    claimed_hash = _digest(artifact.artifact_hash, "artifact_hash")
    expected_hash = _hash(artifact.to_payload(include_hash=False))
    if not hmac.compare_digest(claimed_hash, expected_hash):
        raise ValueError("Team Rating artifact hash does not match")


def _config_from_payload(value: object) -> TeamRatingConfig:
    row = _exact_object(value, _CONFIG_FIELDS, "Team Rating config")
    return TeamRatingConfig(
        initial_rating=row["initial_rating"],
        scale=row["scale"],
        k_factor=row["k_factor"],
        inactivity_half_life_days=row["inactivity_half_life_days"],
        roster_carry_power=row["roster_carry_power"],
        radiant_side_logit=row["radiant_side_logit"],
        config_version=row["config_version"],
    )


def _map_from_payload(value: object) -> RatingMapInput:
    row = _exact_object(value, _MAP_FIELDS, "Team Rating corpus row")
    result_usable_at = row["result_usable_at"]
    return RatingMapInput(
        match_id=row["match_id"],
        series_id=_optional_int(row["series_id"], "series_id"),
        event_id=row["event_id"],
        started_at=_parse_utc(row["started_at"], "started_at"),
        completed_at=_parse_utc(row["completed_at"], "completed_at"),
        result_usable_at=(
            None
            if result_usable_at is None
            else _parse_utc(result_usable_at, "result_usable_at")
        ),
        radiant_team_id=row["radiant_team_id"],
        dire_team_id=row["dire_team_id"],
        radiant_roster=tuple(_array(row["radiant_roster"], "radiant_roster")),
        dire_roster=tuple(_array(row["dire_roster"], "dire_roster")),
        radiant_win=row["radiant_win"],
    )


def _state_from_payload(value: object) -> TeamRatingState:
    row = _exact_object(value, _STATE_FIELDS, "Team Rating state")
    observed_at = row["last_observed_at"]
    return TeamRatingState(
        team_id=row["team_id"],
        rating=row["rating"],
        maps_seen=row["maps_seen"],
        roster=tuple(_array(row["roster"], "state roster")),
        last_observed_at=(
            None if observed_at is None else _parse_utc(observed_at, "last_observed_at")
        ),
    )


def _target_from_payload(value: object) -> TeamRatingTarget:
    row = _exact_object(value, _TARGET_FIELDS, "Team Rating target")
    return TeamRatingTarget(
        match_id=row["match_id"],
        series_id=_optional_int(row["series_id"], "target series_id"),
        event_id=row["event_id"],
        started_at=_parse_utc(row["started_at"], "target started_at"),
        radiant_team_id=row["radiant_team_id"],
        dire_team_id=row["dire_team_id"],
        radiant_roster=tuple(_array(row["radiant_roster"], "target radiant_roster")),
        dire_roster=tuple(_array(row["dire_roster"], "target dire_roster")),
    )


def _prediction_from_payload(value: object) -> TeamRatingPrediction:
    row = _exact_object(value, _PREDICTION_FIELDS, "Team Rating prediction")
    return TeamRatingPrediction(
        match_id=row["match_id"],
        prediction_cutoff=_parse_utc(
            row["prediction_cutoff"],
            "prediction_cutoff",
        ),
        radiant_rating=row["radiant_rating"],
        dire_rating=row["dire_rating"],
        rating_diff=row["rating_diff"],
        radiant_side_logit=row["radiant_side_logit"],
        raw_probability=row["raw_probability"],
        radiant_roster_continuity=row["radiant_roster_continuity"],
        dire_roster_continuity=row["dire_roster_continuity"],
        support=row["support"],
        input_hash=row["input_hash"],
    )


def team_rating_artifact_from_payload(
    payload: Mapping[str, Any],
) -> TeamRatingArtifact:
    row = _exact_object(payload, _ARTIFACT_FIELDS, "Team Rating artifact")
    if row["schema"] != TEAM_RATING_ARTIFACT_SCHEMA:
        raise ValueError("unsupported Team Rating artifact schema")
    if row["artifact_version"] != TEAM_RATING_ARTIFACT_VERSION:
        raise ValueError("unsupported Team Rating artifact version")
    if row["rating_version"] != TEAM_RATING_VERSION:
        raise ValueError("unsupported Team Rating version")
    corpus = tuple(
        _map_from_payload(value)
        for value in _array(
            row["ordered_training_corpus"],
            "ordered_training_corpus",
        )
    )
    states = tuple(
        _state_from_payload(value)
        for value in _array(row["state_before_target"], "state_before_target")
    )
    runtime_raw = _exact_object(
        row["runtime_versions"],
        frozenset(name for name, _version in TEAM_RATING_RUNTIME_VERSIONS),
        "Team Rating runtime_versions",
    )
    runtime_versions = tuple(
        (name, runtime_raw[name]) for name, _version in TEAM_RATING_RUNTIME_VERSIONS
    )
    artifact = TeamRatingArtifact(
        artifact_version=row["artifact_version"],
        rating_version=row["rating_version"],
        config=_config_from_payload(row["config"]),
        training_cutoff=_parse_utc(row["training_cutoff"], "training_cutoff"),
        ordered_training_corpus=corpus,
        training_input_hash=_digest(
            row["training_input_hash"],
            "training_input_hash",
        ),
        state_before_target=states,
        target=_target_from_payload(row["target"]),
        prediction=_prediction_from_payload(row["prediction"]),
        runtime_versions=runtime_versions,
        artifact_hash=_digest(row["artifact_hash"], "artifact_hash"),
    )
    verify_team_rating_artifact(artifact)
    if canonical_json_bytes(artifact.to_payload()) != canonical_json_bytes(dict(row)):
        raise ValueError("Team Rating artifact payload is not canonical")
    return artifact


def load_team_rating_artifact_json(payload_json: str) -> TeamRatingArtifact:
    return team_rating_artifact_from_payload(_strict_json_object(payload_json))


def assert_team_rating_artifact_deployable(artifact: TeamRatingArtifact) -> None:
    verify_team_rating_artifact(artifact)


__all__ = [
    "TEAM_RATING_ARTIFACT_SCHEMA",
    "TEAM_RATING_ARTIFACT_VERSION",
    "TEAM_RATING_RUNTIME_VERSIONS",
    "TeamRatingArtifact",
    "assert_team_rating_artifact_deployable",
    "build_team_rating_artifact",
    "load_team_rating_artifact_json",
    "team_rating_artifact_from_payload",
    "verify_team_rating_artifact",
]
