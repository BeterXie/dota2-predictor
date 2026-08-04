"""Deterministic, causal Team Rating for prematch Radiant probability."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from numbers import Real
from typing import Any, Iterable, Mapping

from .raw_archive import canonical_json_bytes


TEAM_RATING_VERSION = "team-rating-elo-v1"

SCALE_GRID = (200.0, 300.0, 400.0)
K_FACTOR_GRID = (8.0, 16.0, 24.0, 32.0)
INACTIVITY_HALF_LIFE_DAYS_GRID = (None, 90.0, 180.0, 365.0)
ROSTER_CARRY_POWER_GRID = (0.5, 1.0, 2.0)

UTC = timezone.utc


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _utc(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_roster(value: object, field: str) -> tuple[int, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{field} must be a roster sequence")
    players = tuple(
        _positive_int(player_id, f"{field} player_id") for player_id in value
    )
    roster = tuple(sorted(set(players)))
    if roster and len(roster) != 5:
        raise ValueError(f"{field} must be empty or contain exactly five players")
    return roster


def _digest(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _continuity(value: object, field: str) -> float | None:
    if value is None:
        return None
    result = _finite(value, field)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{field} must be between 0 and 1")
    return result


@dataclass(frozen=True)
class TeamRatingConfig:
    initial_rating: float
    scale: float
    k_factor: float
    inactivity_half_life_days: float | None
    roster_carry_power: float
    radiant_side_logit: float
    config_version: str

    def __post_init__(self) -> None:
        initial = _finite(self.initial_rating, "initial_rating")
        scale = _finite(self.scale, "scale")
        k_factor = _finite(self.k_factor, "k_factor")
        carry_power = _finite(self.roster_carry_power, "roster_carry_power")
        side_logit = _finite(self.radiant_side_logit, "radiant_side_logit")
        half_life = (
            None
            if self.inactivity_half_life_days is None
            else _finite(
                self.inactivity_half_life_days,
                "inactivity_half_life_days",
            )
        )
        if scale <= 0.0:
            raise ValueError("scale must be positive")
        if k_factor <= 0.0:
            raise ValueError("k_factor must be positive")
        if carry_power <= 0.0:
            raise ValueError("roster_carry_power must be positive")
        if half_life is not None and half_life <= 0.0:
            raise ValueError("inactivity_half_life_days must be positive")
        if self.config_version != TEAM_RATING_VERSION:
            raise ValueError("unsupported Team Rating config version")
        object.__setattr__(self, "initial_rating", initial)
        object.__setattr__(self, "scale", scale)
        object.__setattr__(self, "k_factor", k_factor)
        object.__setattr__(self, "inactivity_half_life_days", half_life)
        object.__setattr__(self, "roster_carry_power", carry_power)
        object.__setattr__(self, "radiant_side_logit", side_logit)

    def to_payload(self) -> dict[str, Any]:
        return {
            "initial_rating": self.initial_rating,
            "scale": self.scale,
            "k_factor": self.k_factor,
            "inactivity_half_life_days": self.inactivity_half_life_days,
            "roster_carry_power": self.roster_carry_power,
            "radiant_side_logit": self.radiant_side_logit,
            "config_version": self.config_version,
        }


@dataclass(frozen=True)
class RatingMapInput:
    match_id: int
    series_id: int | None
    event_id: str
    started_at: datetime
    completed_at: datetime
    result_usable_at: datetime | None
    radiant_team_id: int
    dire_team_id: int
    radiant_roster: tuple[int, ...]
    dire_roster: tuple[int, ...]
    radiant_win: bool

    def __post_init__(self) -> None:
        _positive_int(self.match_id, "match_id")
        if self.series_id is not None:
            _positive_int(self.series_id, "series_id")
        _nonempty(self.event_id, "event_id")
        started = _utc(self.started_at, "started_at")
        completed = _utc(self.completed_at, "completed_at")
        usable = (
            None
            if self.result_usable_at is None
            else _utc(self.result_usable_at, "result_usable_at")
        )
        if completed <= started:
            raise ValueError("completed_at must be after started_at")
        if usable is not None and usable < completed:
            raise ValueError("result_usable_at cannot precede completed_at")
        radiant_team_id = _positive_int(self.radiant_team_id, "radiant_team_id")
        dire_team_id = _positive_int(self.dire_team_id, "dire_team_id")
        if radiant_team_id == dire_team_id:
            raise ValueError("radiant and dire team IDs must differ")
        if not isinstance(self.radiant_win, bool):
            raise ValueError("radiant_win must be boolean")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "completed_at", completed)
        object.__setattr__(self, "result_usable_at", usable)
        object.__setattr__(
            self,
            "radiant_roster",
            _canonical_roster(self.radiant_roster, "radiant_roster"),
        )
        object.__setattr__(
            self,
            "dire_roster",
            _canonical_roster(self.dire_roster, "dire_roster"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "series_id": self.series_id,
            "event_id": self.event_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "result_usable_at": (
                None
                if self.result_usable_at is None
                else self.result_usable_at.isoformat()
            ),
            "radiant_team_id": self.radiant_team_id,
            "dire_team_id": self.dire_team_id,
            "radiant_roster": list(self.radiant_roster),
            "dire_roster": list(self.dire_roster),
            "radiant_win": self.radiant_win,
        }


@dataclass(frozen=True)
class TeamRatingState:
    team_id: int
    rating: float
    maps_seen: int
    roster: tuple[int, ...]
    last_observed_at: datetime | None

    def __post_init__(self) -> None:
        _positive_int(self.team_id, "team_id")
        object.__setattr__(self, "rating", _finite(self.rating, "rating"))
        _nonnegative_int(self.maps_seen, "maps_seen")
        object.__setattr__(self, "roster", _canonical_roster(self.roster, "roster"))
        object.__setattr__(
            self,
            "last_observed_at",
            (
                None
                if self.last_observed_at is None
                else _utc(self.last_observed_at, "last_observed_at")
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "rating": self.rating,
            "maps_seen": self.maps_seen,
            "roster": list(self.roster),
            "last_observed_at": (
                None
                if self.last_observed_at is None
                else self.last_observed_at.isoformat()
            ),
        }


@dataclass(frozen=True)
class TeamRatingTarget:
    match_id: int
    series_id: int | None
    event_id: str
    started_at: datetime
    radiant_team_id: int
    dire_team_id: int
    radiant_roster: tuple[int, ...]
    dire_roster: tuple[int, ...]

    def __post_init__(self) -> None:
        _positive_int(self.match_id, "target match_id")
        if self.series_id is not None:
            _positive_int(self.series_id, "target series_id")
        _nonempty(self.event_id, "target event_id")
        object.__setattr__(
            self, "started_at", _utc(self.started_at, "target started_at")
        )
        radiant_team_id = _positive_int(
            self.radiant_team_id,
            "target radiant_team_id",
        )
        dire_team_id = _positive_int(self.dire_team_id, "target dire_team_id")
        if radiant_team_id == dire_team_id:
            raise ValueError("target radiant and dire team IDs must differ")
        object.__setattr__(
            self,
            "radiant_roster",
            _canonical_roster(self.radiant_roster, "target radiant_roster"),
        )
        object.__setattr__(
            self,
            "dire_roster",
            _canonical_roster(self.dire_roster, "target dire_roster"),
        )

    @classmethod
    def from_map(cls, value: RatingMapInput) -> TeamRatingTarget:
        if not isinstance(value, RatingMapInput):
            raise ValueError("target must be a RatingMapInput")
        return cls(
            match_id=value.match_id,
            series_id=value.series_id,
            event_id=value.event_id,
            started_at=value.started_at,
            radiant_team_id=value.radiant_team_id,
            dire_team_id=value.dire_team_id,
            radiant_roster=value.radiant_roster,
            dire_roster=value.dire_roster,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "series_id": self.series_id,
            "event_id": self.event_id,
            "started_at": self.started_at.isoformat(),
            "radiant_team_id": self.radiant_team_id,
            "dire_team_id": self.dire_team_id,
            "radiant_roster": list(self.radiant_roster),
            "dire_roster": list(self.dire_roster),
        }


@dataclass(frozen=True)
class TeamRatingPrediction:
    match_id: int
    prediction_cutoff: datetime
    radiant_rating: float
    dire_rating: float
    rating_diff: float
    radiant_side_logit: float
    raw_probability: float
    radiant_roster_continuity: float | None
    dire_roster_continuity: float | None
    support: int
    input_hash: str

    def __post_init__(self) -> None:
        _positive_int(self.match_id, "prediction match_id")
        object.__setattr__(
            self,
            "prediction_cutoff",
            _utc(self.prediction_cutoff, "prediction_cutoff"),
        )
        radiant = _finite(self.radiant_rating, "radiant_rating")
        dire = _finite(self.dire_rating, "dire_rating")
        difference = _finite(self.rating_diff, "rating_diff")
        if not math.isclose(difference, radiant - dire, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("rating_diff does not match team ratings")
        probability = _finite(self.raw_probability, "raw_probability")
        if not 0.0 <= probability <= 1.0:
            raise ValueError("raw_probability must be between 0 and 1")
        object.__setattr__(self, "radiant_rating", radiant)
        object.__setattr__(self, "dire_rating", dire)
        object.__setattr__(self, "rating_diff", difference)
        object.__setattr__(
            self,
            "radiant_side_logit",
            _finite(self.radiant_side_logit, "prediction radiant_side_logit"),
        )
        object.__setattr__(self, "raw_probability", probability)
        object.__setattr__(
            self,
            "radiant_roster_continuity",
            _continuity(
                self.radiant_roster_continuity,
                "radiant_roster_continuity",
            ),
        )
        object.__setattr__(
            self,
            "dire_roster_continuity",
            _continuity(self.dire_roster_continuity, "dire_roster_continuity"),
        )
        _nonnegative_int(self.support, "prediction support")
        object.__setattr__(self, "input_hash", _sha256(self.input_hash, "input_hash"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "prediction_cutoff": self.prediction_cutoff.isoformat(),
            "radiant_rating": self.radiant_rating,
            "dire_rating": self.dire_rating,
            "rating_diff": self.rating_diff,
            "radiant_side_logit": self.radiant_side_logit,
            "raw_probability": self.raw_probability,
            "radiant_roster_continuity": self.radiant_roster_continuity,
            "dire_roster_continuity": self.dire_roster_continuity,
            "support": self.support,
            "input_hash": self.input_hash,
        }


RatingStates = Mapping[int, TeamRatingState] | Iterable[TeamRatingState]


def canonical_rating_states(states: RatingStates) -> tuple[TeamRatingState, ...]:
    if isinstance(states, Mapping):
        values = tuple(states.values())
        if any(
            not isinstance(team_id, int)
            or not isinstance(state, TeamRatingState)
            or state.team_id != team_id
            for team_id, state in states.items()
        ):
            raise ValueError("state mapping keys must match Team Rating state IDs")
    else:
        values = tuple(states)
    by_team: dict[int, TeamRatingState] = {}
    for state in values:
        if not isinstance(state, TeamRatingState):
            raise ValueError("states must contain TeamRatingState values")
        existing = by_team.get(state.team_id)
        if existing is not None and existing != state:
            raise ValueError(f"conflicting states for team {state.team_id}")
        by_team[state.team_id] = state
    return tuple(by_team[team_id] for team_id in sorted(by_team))


def estimate_radiant_side_logit(radiant_wins: int, maps: int) -> float:
    wins = _nonnegative_int(radiant_wins, "radiant_wins")
    support = _nonnegative_int(maps, "maps")
    if wins > support:
        raise ValueError("radiant_wins cannot exceed maps")
    probability = (wins + 1.0) / (support + 2.0)
    return math.log(probability / (1.0 - probability))


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def team_rating_probability(
    radiant_rating: float,
    dire_rating: float,
    config: TeamRatingConfig,
) -> float:
    if not isinstance(config, TeamRatingConfig):
        raise ValueError("config must be a TeamRatingConfig")
    radiant = _finite(radiant_rating, "radiant_rating")
    dire = _finite(dire_rating, "dire_rating")
    rating_logit = math.log(10.0) / config.scale * (radiant - dire)
    return _sigmoid(rating_logit + config.radiant_side_logit)


def roster_continuity(
    previous_roster: tuple[int, ...],
    current_roster: tuple[int, ...],
) -> float | None:
    previous = _canonical_roster(previous_roster, "previous_roster")
    current = _canonical_roster(current_roster, "current_roster")
    if not previous or not current:
        return None
    return len(set(previous) & set(current)) / 5.0


def effective_team_rating(
    state: TeamRatingState,
    current_roster: tuple[int, ...],
    as_of: datetime,
    config: TeamRatingConfig,
) -> tuple[float, float | None]:
    if not isinstance(state, TeamRatingState):
        raise ValueError("state must be a TeamRatingState")
    if not isinstance(config, TeamRatingConfig):
        raise ValueError("config must be a TeamRatingConfig")
    cutoff = _utc(as_of, "rating cutoff")
    rating = state.rating
    if state.last_observed_at is not None:
        inactive_seconds = (cutoff - state.last_observed_at).total_seconds()
        if inactive_seconds < 0.0:
            raise ValueError("rating cutoff cannot precede last_observed_at")
        if config.inactivity_half_life_days is not None:
            inactive_days = inactive_seconds / 86_400.0
            decay = math.exp(
                -math.log(2.0) * inactive_days / config.inactivity_half_life_days
            )
            rating = config.initial_rating + decay * (rating - config.initial_rating)
    continuity = roster_continuity(state.roster, current_roster)
    if continuity is not None:
        carry = continuity**config.roster_carry_power
        rating = config.initial_rating + carry * (rating - config.initial_rating)
    return _finite(rating, "effective rating"), continuity


def _default_state(team_id: int, config: TeamRatingConfig) -> TeamRatingState:
    return TeamRatingState(team_id, config.initial_rating, 0, (), None)


def _state_index(
    states: RatingStates,
) -> tuple[tuple[TeamRatingState, ...], dict[int, TeamRatingState]]:
    ordered = canonical_rating_states(states)
    return ordered, {state.team_id: state for state in ordered}


def predict_team_rating(
    states: RatingStates,
    target: RatingMapInput | TeamRatingTarget,
    prediction_cutoff: datetime,
    config: TeamRatingConfig,
) -> TeamRatingPrediction:
    if not isinstance(config, TeamRatingConfig):
        raise ValueError("config must be a TeamRatingConfig")
    target_value = (
        TeamRatingTarget.from_map(target)
        if isinstance(target, RatingMapInput)
        else target
    )
    if not isinstance(target_value, TeamRatingTarget):
        raise ValueError("target must be a RatingMapInput or TeamRatingTarget")
    cutoff = _utc(prediction_cutoff, "prediction_cutoff")
    if cutoff > target_value.started_at:
        raise ValueError("prediction_cutoff cannot follow target started_at")
    _ordered, index = _state_index(states)
    radiant_state = index.get(
        target_value.radiant_team_id,
        _default_state(target_value.radiant_team_id, config),
    )
    dire_state = index.get(
        target_value.dire_team_id,
        _default_state(target_value.dire_team_id, config),
    )
    radiant_rating, radiant_continuity = effective_team_rating(
        radiant_state,
        target_value.radiant_roster,
        cutoff,
        config,
    )
    dire_rating, dire_continuity = effective_team_rating(
        dire_state,
        target_value.dire_roster,
        cutoff,
        config,
    )
    probability = team_rating_probability(radiant_rating, dire_rating, config)
    input_hash = _digest(
        {
            "rating_version": TEAM_RATING_VERSION,
            "config": config.to_payload(),
            "prediction_cutoff": cutoff.isoformat(),
            "target": target_value.to_payload(),
            "states": [radiant_state.to_payload(), dire_state.to_payload()],
        }
    )
    return TeamRatingPrediction(
        match_id=target_value.match_id,
        prediction_cutoff=cutoff,
        radiant_rating=radiant_rating,
        dire_rating=dire_rating,
        rating_diff=radiant_rating - dire_rating,
        radiant_side_logit=config.radiant_side_logit,
        raw_probability=probability,
        radiant_roster_continuity=radiant_continuity,
        dire_roster_continuity=dire_continuity,
        support=radiant_state.maps_seen + dire_state.maps_seen,
        input_hash=input_hash,
    )


def update_team_ratings(
    states: RatingStates,
    row: RatingMapInput,
    update_cutoff: datetime,
    config: TeamRatingConfig,
) -> tuple[TeamRatingState, ...]:
    if not isinstance(row, RatingMapInput):
        raise ValueError("row must be a RatingMapInput")
    if not isinstance(config, TeamRatingConfig):
        raise ValueError("config must be a TeamRatingConfig")
    cutoff = _utc(update_cutoff, "update_cutoff")
    ordered, index = _state_index(states)
    if row.result_usable_at is None or row.result_usable_at > cutoff:
        return ordered
    rating_as_of = row.started_at
    radiant_state = index.get(
        row.radiant_team_id,
        _default_state(row.radiant_team_id, config),
    )
    dire_state = index.get(
        row.dire_team_id,
        _default_state(row.dire_team_id, config),
    )
    radiant_rating, _radiant_continuity = effective_team_rating(
        radiant_state,
        row.radiant_roster,
        rating_as_of,
        config,
    )
    dire_rating, _dire_continuity = effective_team_rating(
        dire_state,
        row.dire_roster,
        rating_as_of,
        config,
    )
    expected = team_rating_probability(radiant_rating, dire_rating, config)
    error = float(row.radiant_win) - expected
    index[row.radiant_team_id] = TeamRatingState(
        team_id=row.radiant_team_id,
        rating=radiant_rating + config.k_factor * error,
        maps_seen=radiant_state.maps_seen + 1,
        roster=row.radiant_roster,
        last_observed_at=row.completed_at,
    )
    index[row.dire_team_id] = TeamRatingState(
        team_id=row.dire_team_id,
        rating=dire_rating - config.k_factor * error,
        maps_seen=dire_state.maps_seen + 1,
        roster=row.dire_roster,
        last_observed_at=row.completed_at,
    )
    return tuple(index[team_id] for team_id in sorted(index))


def canonical_training_corpus(
    rows: Iterable[RatingMapInput],
    training_cutoff: datetime,
) -> tuple[RatingMapInput, ...]:
    cutoff = _utc(training_cutoff, "training_cutoff")
    by_match: dict[int, RatingMapInput] = {}
    for row in rows:
        if not isinstance(row, RatingMapInput):
            raise ValueError("training corpus must contain RatingMapInput values")
        existing = by_match.get(row.match_id)
        if existing is not None and canonical_json_bytes(
            existing.to_payload()
        ) != canonical_json_bytes(row.to_payload()):
            raise ValueError(f"conflicting training rows for match {row.match_id}")
        by_match[row.match_id] = row
    eligible = (
        row
        for row in by_match.values()
        if row.result_usable_at is not None and row.result_usable_at <= cutoff
    )
    return tuple(
        sorted(
            eligible,
            key=lambda row: (
                row.started_at,
                row.completed_at,
                row.match_id,
                row.event_id,
            ),
        )
    )


def rating_training_input_hash(
    rows: Iterable[RatingMapInput],
    training_cutoff: datetime,
    config: TeamRatingConfig,
) -> str:
    cutoff = _utc(training_cutoff, "training_cutoff")
    corpus = canonical_training_corpus(rows, cutoff)
    return _digest(
        {
            "rating_version": TEAM_RATING_VERSION,
            "config": config.to_payload(),
            "training_cutoff": cutoff.isoformat(),
            "ordered_training_corpus": [row.to_payload() for row in corpus],
        }
    )


def replay_team_ratings(
    rows: Iterable[RatingMapInput],
    training_cutoff: datetime,
    config: TeamRatingConfig,
) -> tuple[TeamRatingState, ...]:
    cutoff = _utc(training_cutoff, "training_cutoff")
    corpus = canonical_training_corpus(rows, cutoff)
    states: tuple[TeamRatingState, ...] = ()
    for row in corpus:
        if row.result_usable_at is None:
            raise AssertionError(
                "canonical training corpus contains an unset result time"
            )
        states = update_team_ratings(states, row, row.result_usable_at, config)
    return states


__all__ = [
    "INACTIVITY_HALF_LIFE_DAYS_GRID",
    "K_FACTOR_GRID",
    "ROSTER_CARRY_POWER_GRID",
    "SCALE_GRID",
    "TEAM_RATING_VERSION",
    "RatingMapInput",
    "TeamRatingConfig",
    "TeamRatingPrediction",
    "TeamRatingState",
    "TeamRatingTarget",
    "canonical_rating_states",
    "canonical_training_corpus",
    "effective_team_rating",
    "estimate_radiant_side_logit",
    "predict_team_rating",
    "rating_training_input_hash",
    "replay_team_ratings",
    "roster_continuity",
    "team_rating_probability",
    "update_team_ratings",
]
