"""Formal-corpus nested chronological evaluation for Team Rating."""

from __future__ import annotations

import hashlib
import hmac
import math
import random
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from scipy.special import expit

from database.session import DatabaseRow, PostgresSession

from .draft_features import AvailabilityMode
from .draft_model import BinaryMetrics, evaluate_binary_predictions
from .raw_archive import canonical_json_bytes
from .team_rating import (
    INACTIVITY_HALF_LIFE_DAYS_GRID,
    K_FACTOR_GRID,
    ROSTER_CARRY_POWER_GRID,
    SCALE_GRID,
    RatingMapInput,
    TeamRatingConfig,
    canonical_training_corpus,
    estimate_radiant_side_logit,
)
from .team_rating_artifacts import TeamRatingArtifact, build_team_rating_artifact


UTC = timezone.utc
TEAM_RATING_BACKTEST_VERSION = "team-rating-walk-forward-v1"
BOOTSTRAP_SAMPLES = 1_000
CALIBRATION_BINS = 5
BOOTSTRAP_ALGORITHM_VERSION = "series-cluster-percentile-v1"
BOOTSTRAP_SEED_MATERIAL = f"{TEAM_RATING_BACKTEST_VERSION}:b0-b1-b2"


def _hash(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _canonical_json(payload: object) -> str:
    return canonical_json_bytes(payload).decode("utf-8")


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _utc(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_utc(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc(value, field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    return _utc(parsed, field)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


@dataclass(frozen=True)
class TeamRatingParameters:
    scale: float
    k_factor: float
    inactivity_half_life_days: float | None
    roster_carry_power: float

    def __post_init__(self) -> None:
        TeamRatingConfig(
            initial_rating=1_500.0,
            scale=self.scale,
            k_factor=self.k_factor,
            inactivity_half_life_days=self.inactivity_half_life_days,
            roster_carry_power=self.roster_carry_power,
            radiant_side_logit=0.0,
            config_version="team-rating-elo-v1",
        )

    def to_payload(self) -> dict[str, float | None]:
        return {
            "scale": float(self.scale),
            "k_factor": float(self.k_factor),
            "inactivity_half_life_days": (
                None
                if self.inactivity_half_life_days is None
                else float(self.inactivity_half_life_days)
            ),
            "roster_carry_power": float(self.roster_carry_power),
        }

    def complexity_key(self) -> tuple[int, int]:
        return (
            int(self.inactivity_half_life_days is not None),
            int(self.roster_carry_power != 1.0),
        )


TEAM_RATING_PARAMETER_GRID = tuple(
    TeamRatingParameters(scale, k_factor, half_life, carry_power)
    for scale in SCALE_GRID
    for k_factor in K_FACTOR_GRID
    for half_life in INACTIVITY_HALF_LIFE_DAYS_GRID
    for carry_power in ROSTER_CARRY_POWER_GRID
)


@dataclass(frozen=True)
class TeamRatingSourceAuthority:
    match_id: int
    artifact_id: str
    content_hash: str
    artifact_usable_at: datetime | None
    observation_usable_at: datetime | None

    def __post_init__(self) -> None:
        _positive_int(self.match_id, "source authority match_id")
        if (
            not isinstance(self.artifact_id, str)
            or not self.artifact_id
            or self.artifact_id != self.artifact_id.strip()
        ):
            raise ValueError("source authority artifact_id must be non-empty")
        object.__setattr__(
            self,
            "content_hash",
            _sha256(self.content_hash, "source authority content_hash"),
        )
        if self.artifact_usable_at is not None:
            object.__setattr__(
                self,
                "artifact_usable_at",
                _utc(self.artifact_usable_at, "artifact first_usable_at"),
            )
        if self.observation_usable_at is not None:
            object.__setattr__(
                self,
                "observation_usable_at",
                _utc(self.observation_usable_at, "observation first_usable_at"),
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "match_id": self.match_id,
            "artifact_id": self.artifact_id,
            "content_hash": self.content_hash,
            "artifact_usable_at": (
                None
                if self.artifact_usable_at is None
                else self.artifact_usable_at.isoformat()
            ),
            "observation_usable_at": (
                None
                if self.observation_usable_at is None
                else self.observation_usable_at.isoformat()
            ),
        }


def team_rating_authority_fingerprint(
    *,
    target_source: TeamRatingSourceAuthority,
    ordered_training_sources: Sequence[TeamRatingSourceAuthority],
) -> str:
    if not isinstance(target_source, TeamRatingSourceAuthority):
        raise ValueError("target_source must be a TeamRatingSourceAuthority")
    sources = tuple(ordered_training_sources)
    if any(not isinstance(source, TeamRatingSourceAuthority) for source in sources):
        raise ValueError(
            "ordered_training_sources must contain TeamRatingSourceAuthority values"
        )
    match_ids = tuple(source.match_id for source in sources)
    if len(set(match_ids)) != len(match_ids):
        raise ValueError("ordered training source match IDs must be unique")
    if target_source.match_id in match_ids:
        raise ValueError("target source cannot enter the training source manifest")
    return _hash(
        {
            "schema": "team-rating-source-authority-manifest/v1",
            "target_source": target_source.to_payload(),
            "ordered_training_sources": [source.to_payload() for source in sources],
        }
    )


def combined_team_rating_training_input_hash(
    *,
    artifact_training_input_hash: str,
    authority_fingerprint: str,
) -> str:
    return _hash(
        {
            "schema": "team-rating-combined-training-input/v1",
            "artifact_training_input_hash": _sha256(
                artifact_training_input_hash,
                "artifact_training_input_hash",
            ),
            "authority_fingerprint": _sha256(
                authority_fingerprint,
                "authority_fingerprint",
            ),
        }
    )


def team_rating_run_id(
    *,
    availability_mode: AvailabilityMode | str,
    artifact_hash: str,
    authority_fingerprint: str,
) -> str:
    try:
        mode = (
            availability_mode
            if isinstance(availability_mode, AvailabilityMode)
            else AvailabilityMode(availability_mode)
        )
    except (TypeError, ValueError) as error:
        raise ValueError("unsupported Team Rating availability mode") from error
    return _hash(
        {
            "backtest_version": TEAM_RATING_BACKTEST_VERSION,
            "availability_mode": mode.value,
            "artifact_hash": _sha256(artifact_hash, "artifact_hash"),
            "authority_fingerprint": _sha256(
                authority_fingerprint,
                "authority_fingerprint",
            ),
        }
    )


@dataclass(frozen=True)
class LoadedTeamRatingMap:
    row: RatingMapInput
    prediction_cutoff: datetime | None
    cutoff_source: str | None
    source_authority: TeamRatingSourceAuthority

    def __post_init__(self) -> None:
        if not isinstance(self.row, RatingMapInput):
            raise ValueError("row must be a RatingMapInput")
        if not isinstance(self.source_authority, TeamRatingSourceAuthority):
            raise ValueError("source_authority must be a TeamRatingSourceAuthority")
        if self.source_authority.match_id != self.row.match_id:
            raise ValueError("source authority and map match IDs disagree")
        if (self.prediction_cutoff is None) != (self.cutoff_source is None):
            raise ValueError("prediction cutoff and source must both be set or absent")
        if self.prediction_cutoff is not None:
            cutoff = _utc(self.prediction_cutoff, "prediction_cutoff")
            if cutoff > self.row.started_at:
                raise ValueError("prediction_cutoff cannot follow map start")
            object.__setattr__(self, "prediction_cutoff", cutoff)
            if not isinstance(self.cutoff_source, str) or not self.cutoff_source:
                raise ValueError("cutoff_source must be non-empty")


@dataclass(frozen=True)
class TeamRatingCorpus:
    availability_mode: str
    formal_maps: int
    maps: tuple[LoadedTeamRatingMap, ...]

    def __post_init__(self) -> None:
        mode = AvailabilityMode(self.availability_mode)
        if self.formal_maps < 0 or self.formal_maps != len(self.maps):
            raise ValueError("formal map count does not match loaded corpus")
        match_ids = tuple(row.row.match_id for row in self.maps)
        if len(set(match_ids)) != len(match_ids):
            raise ValueError("Team Rating corpus match IDs must be unique")
        if mode is AvailabilityMode.RECONSTRUCTED:
            if any(
                row.prediction_cutoff != row.row.started_at
                or row.cutoff_source != "reconstructed_map_start"
                for row in self.maps
            ):
                raise ValueError("reconstructed corpus requires map-start targets")
        elif any(row.prediction_cutoff is not None for row in self.maps):
            raise ValueError(
                "formal post-match authority cannot create prospective targets"
            )

    @property
    def targets(self) -> tuple[LoadedTeamRatingMap, ...]:
        return tuple(row for row in self.maps if row.prediction_cutoff is not None)


@dataclass(frozen=True)
class ParameterSelection:
    parameters: TeamRatingParameters
    support: int
    log_loss: float | None
    brier_score: float | None

    def to_payload(self) -> dict[str, object]:
        return {
            "parameters": self.parameters.to_payload(),
            "support": self.support,
            "log_loss": self.log_loss,
            "brier_score": self.brier_score,
        }


@dataclass(frozen=True)
class TeamRatingWalkForwardRun:
    run_id: str
    availability_mode: str
    cutoff_source: str
    selection: ParameterSelection
    config: TeamRatingConfig
    artifact: TeamRatingArtifact
    target_source_authority: TeamRatingSourceAuthority
    ordered_training_sources: tuple[TeamRatingSourceAuthority, ...]
    authority_fingerprint: str
    combined_training_input_hash: str
    series_id: int | None
    event_id: str
    eventual_radiant_win: bool
    radiant_prior_probability: float
    status: str

    def __post_init__(self) -> None:
        mode = AvailabilityMode(self.availability_mode)
        if mode is not AvailabilityMode.RECONSTRUCTED:
            raise ValueError("M2 Team Rating runs must be reconstructed walk-forward")
        if self.cutoff_source != "reconstructed_map_start":
            raise ValueError("reconstructed Team Rating run has invalid cutoff source")
        if self.config != self.artifact.config:
            raise ValueError("walk-forward config does not match its artifact")
        if self.target_source_authority.match_id != self.artifact.target.match_id:
            raise ValueError("target source authority does not match artifact target")
        source_match_ids = tuple(
            source.match_id for source in self.ordered_training_sources
        )
        corpus_match_ids = tuple(
            row.match_id for row in self.artifact.ordered_training_corpus
        )
        if source_match_ids != corpus_match_ids:
            raise ValueError("training source manifest does not match artifact corpus")
        expected_authority = team_rating_authority_fingerprint(
            target_source=self.target_source_authority,
            ordered_training_sources=self.ordered_training_sources,
        )
        if not hmac.compare_digest(
            _sha256(self.authority_fingerprint, "authority_fingerprint"),
            expected_authority,
        ):
            raise ValueError("Team Rating authority fingerprint does not recompute")
        expected_combined = combined_team_rating_training_input_hash(
            artifact_training_input_hash=self.artifact.training_input_hash,
            authority_fingerprint=expected_authority,
        )
        if not hmac.compare_digest(
            _sha256(
                self.combined_training_input_hash,
                "combined_training_input_hash",
            ),
            expected_combined,
        ):
            raise ValueError("combined Team Rating training input hash does not recompute")
        expected_run_id = team_rating_run_id(
            availability_mode=mode,
            artifact_hash=self.artifact.artifact_hash,
            authority_fingerprint=expected_authority,
        )
        if not hmac.compare_digest(_sha256(self.run_id, "run_id"), expected_run_id):
            raise ValueError("Team Rating run_id does not recompute")


@dataclass(frozen=True)
class BootstrapInterval:
    lower: float | None
    upper: float | None


@dataclass(frozen=True)
class MetricIntervals:
    brier_score_90: BootstrapInterval
    brier_score_95: BootstrapInterval
    log_loss_90: BootstrapInterval
    log_loss_95: BootstrapInterval
    ece_90: BootstrapInterval
    ece_95: BootstrapInterval
    auc_90: BootstrapInterval
    auc_95: BootstrapInterval
    accuracy_90: BootstrapInterval
    accuracy_95: BootstrapInterval


@dataclass(frozen=True)
class BaselineReport:
    model_name: str
    support: int
    brier_score: float | None
    log_loss: float | None
    ece: float | None
    auc: float | None
    accuracy: float | None
    intervals: MetricIntervals


@dataclass(frozen=True)
class PairedDeltaReport:
    comparison: str
    metric: str
    delta: float | None
    ci_90: BootstrapInterval
    ci_95: BootstrapInterval
    probability_of_improvement: float | None


@dataclass(frozen=True)
class GateReport:
    status: str
    failures: tuple[str, ...]


@dataclass(frozen=True)
class TeamRatingBacktestReport:
    backtest_version: str
    bootstrap_algorithm: str
    bootstrap_seed_material: str
    bootstrap_samples: int
    availability_mode: str
    dry_run: bool
    formal_maps: int
    eligible_targets: int
    evaluated_targets: int
    insufficient_targets: int
    failed_targets: int
    evaluation_coverage: float
    inserted_runs: int
    unchanged_runs: int
    inserted_predictions: int
    unchanged_predictions: int
    inserted_snapshots: int
    unchanged_snapshots: int
    corpus_started_at: str | None
    corpus_completed_at: str | None
    selected_parameter_counts: tuple[tuple[str, int], ...]
    baselines: tuple[BaselineReport, ...]
    paired_deltas: tuple[PairedDeltaReport, ...]
    gate: GateReport


@dataclass(frozen=True)
class _EvaluationPoint:
    match_id: int
    series_id: int | None
    event_id: str
    outcome: bool
    constant_50: float
    radiant_prior: float
    team_rating: float


def _rows_by_match(rows: Iterable[DatabaseRow]) -> dict[int, list[DatabaseRow]]:
    result: dict[int, list[DatabaseRow]] = {}
    for row in rows:
        result.setdefault(int(row["match_id"]), []).append(row)
    return result


def _source_availability(
    source: TeamRatingSourceAuthority,
) -> tuple[datetime | None, str | None]:
    candidates = (
        (
            source.observation_usable_at,
            "raw_observation_first_usable_at",
        ),
        (
            source.artifact_usable_at,
            "raw_artifact_first_usable_at",
        ),
    )
    if any(candidate[0] is None for candidate in candidates):
        return None, None
    return max(candidates, key=lambda candidate: (candidate[0], candidate[1]))


def _roster(
    players: Sequence[DatabaseRow],
    *,
    match_id: int,
    team_id: int,
    radiant: bool,
) -> tuple[int, ...]:
    side_rows = tuple(row for row in players if row["is_radiant"] in (0, 1) and bool(row["is_radiant"]) is radiant)
    if len(side_rows) != 5:
        return ()
    accounts: list[int] = []
    for row in side_rows:
        stored_team_id = row["team_id"]
        if stored_team_id is not None and int(stored_team_id) != team_id:
            raise ValueError(f"formal map {match_id} player team identity disagrees")
        account_id = row["account_id"]
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            return ()
        accounts.append(account_id)
    return tuple(sorted(accounts)) if len(set(accounts)) == 5 else ()


def load_team_rating_corpus(
    connection: PostgresSession,
    *,
    availability_mode: AvailabilityMode,
) -> TeamRatingCorpus:
    """Load exact formal maps under one explicit evidence policy."""

    if not isinstance(availability_mode, AvailabilityMode):
        raise ValueError("availability_mode must be an AvailabilityMode")
    base_rows = connection.execute(
        """SELECT eligible.match_id, eligible.event_id, status.series_id,
                  status.latest_raw_artifact_id, status.latest_raw_content_hash,
                  match.start_time, match.duration, match.radiant_win,
                  match.radiant_team_id, match.dire_team_id,
                  artifact.first_usable_at AS artifact_usable_at,
                  (SELECT MIN(observation.first_usable_at)
                     FROM raw_source_observations AS observation
                    WHERE observation.artifact_id=artifact.artifact_id
                      AND observation.content_hash=artifact.content_hash
                      AND observation.first_usable_at IS NOT NULL
                  ) AS observation_usable_at
             FROM formal_map_eligibility AS eligible
             JOIN match_ingest_status AS status ON status.match_id=eligible.match_id
             JOIN matches AS match ON match.match_id=eligible.match_id
             JOIN raw_source_artifacts AS artifact
               ON artifact.artifact_id=status.latest_raw_artifact_id
              AND artifact.content_hash=status.latest_raw_content_hash
              AND artifact.source='opendota'
            ORDER BY match.start_time, eligible.match_id"""
    ).fetchall()
    formal_maps = int(
        connection.execute("SELECT COUNT(*) FROM formal_map_eligibility").scalar_one()
    )
    if len(base_rows) != formal_maps:
        raise ValueError("a formal map lacks its exact latest OpenDota artifact")
    players_by_match = _rows_by_match(
        connection.execute(
            """SELECT player.match_id, player.account_id, player.player_slot,
                      player.is_radiant, player.team_id
                 FROM formal_map_eligibility AS eligible
                 JOIN match_players AS player ON player.match_id=eligible.match_id
                ORDER BY player.match_id, player.player_slot, player.id"""
        ).fetchall()
    )

    loaded: list[LoadedTeamRatingMap] = []
    for base in base_rows:
        match_id = _positive_int(base["match_id"], "match_id")
        start_time = _positive_int(base["start_time"], f"map {match_id} start_time")
        duration = _positive_int(base["duration"], f"map {match_id} duration")
        radiant_team_id = _positive_int(
            base["radiant_team_id"], f"map {match_id} radiant_team_id"
        )
        dire_team_id = _positive_int(
            base["dire_team_id"], f"map {match_id} dire_team_id"
        )
        if radiant_team_id == dire_team_id:
            raise ValueError(f"formal map {match_id} has identical team identities")
        if base["radiant_win"] not in (0, 1, False, True):
            raise ValueError(f"formal map {match_id} has an invalid result")
        started_at = datetime.fromtimestamp(start_time, UTC)
        completed_at = started_at + timedelta(seconds=duration)
        source_authority = TeamRatingSourceAuthority(
            match_id=match_id,
            artifact_id=str(base["latest_raw_artifact_id"] or ""),
            content_hash=str(base["latest_raw_content_hash"] or ""),
            artifact_usable_at=_parse_utc(
                base["artifact_usable_at"],
                "artifact first_usable_at",
            ),
            observation_usable_at=_parse_utc(
                base["observation_usable_at"],
                "observation first_usable_at",
            ),
        )
        for usable_at in (
            source_authority.artifact_usable_at,
            source_authority.observation_usable_at,
        ):
            if usable_at is not None and usable_at < completed_at:
                raise ValueError(f"formal map {match_id} source precedes completion")
        source_usable_at, _source = _source_availability(source_authority)
        result_usable_at = (
            completed_at
            if availability_mode is AvailabilityMode.RECONSTRUCTED
            else source_usable_at
        )
        players = players_by_match.get(match_id, ())
        row = RatingMapInput(
            match_id=match_id,
            series_id=_optional_positive_int(base["series_id"], "series_id"),
            event_id=str(base["event_id"]),
            started_at=started_at,
            completed_at=completed_at,
            result_usable_at=result_usable_at,
            radiant_team_id=radiant_team_id,
            dire_team_id=dire_team_id,
            radiant_roster=_roster(
                players,
                match_id=match_id,
                team_id=radiant_team_id,
                radiant=True,
            ),
            dire_roster=_roster(
                players,
                match_id=match_id,
                team_id=dire_team_id,
                radiant=False,
            ),
            radiant_win=bool(base["radiant_win"]),
        )
        # The exact OpenDota artifact is post-match authority. It can reconstruct
        # historical targets, but it cannot manufacture a prospective target.
        prediction_cutoff = (
            started_at
            if availability_mode is AvailabilityMode.RECONSTRUCTED
            else None
        )
        cutoff_source = (
            "reconstructed_map_start"
            if availability_mode is AvailabilityMode.RECONSTRUCTED
            else None
        )
        loaded.append(
            LoadedTeamRatingMap(
                row,
                prediction_cutoff,
                cutoff_source,
                source_authority,
            )
        )
    return TeamRatingCorpus(availability_mode.value, formal_maps, tuple(loaded))


def _radiant_prior(rows: Sequence[RatingMapInput]) -> float:
    wins = sum(row.radiant_win for row in rows)
    logit = estimate_radiant_side_logit(wins, len(rows))
    return 1.0 / (1.0 + math.exp(-logit))


def _earlier_training_corpus(
    rows: Iterable[RatingMapInput],
    cutoff: datetime,
) -> tuple[RatingMapInput, ...]:
    at = _utc(cutoff, "training cutoff")
    return tuple(
        row
        for row in canonical_training_corpus(rows, at)
        if row.completed_at < at
    )


def _config(
    parameters: TeamRatingParameters,
    history: Sequence[RatingMapInput],
) -> TeamRatingConfig:
    return TeamRatingConfig(
        initial_rating=1_500.0,
        scale=parameters.scale,
        k_factor=parameters.k_factor,
        inactivity_half_life_days=parameters.inactivity_half_life_days,
        roster_carry_power=parameters.roster_carry_power,
        radiant_side_logit=estimate_radiant_side_logit(
            sum(row.radiant_win for row in history),
            len(history),
        ),
        config_version="team-rating-elo-v1",
    )


def _log_loss(outcome: bool, probability: float) -> float:
    bounded = min(max(probability, 1e-15), 1.0 - 1e-15)
    return -math.log(bounded if outcome else 1.0 - bounded)


def _inner_candidate_probability(
    history: Sequence[RatingMapInput],
    target: RatingMapInput,
    parameters: TeamRatingParameters,
) -> float:
    """Replay primitive state for grid scoring; selected runs use PR-1 replay."""

    initial = 1_500.0
    side_logit = estimate_radiant_side_logit(
        sum(row.radiant_win for row in history),
        len(history),
    )
    rating_scale = math.log(10.0) / parameters.scale
    states: dict[int, tuple[float, tuple[int, ...], datetime | None]] = {}

    def effective(
        team_id: int,
        roster: tuple[int, ...],
        as_of: datetime,
    ) -> float:
        rating, previous_roster, last_observed_at = states.get(
            team_id,
            (initial, (), None),
        )
        if last_observed_at is not None:
            inactive_seconds = (as_of - last_observed_at).total_seconds()
            if inactive_seconds < 0.0:
                raise ValueError("rating cutoff cannot precede last_observed_at")
            if parameters.inactivity_half_life_days is not None:
                inactive_days = inactive_seconds / 86_400.0
                decay = math.exp(
                    -math.log(2.0)
                    * inactive_days
                    / parameters.inactivity_half_life_days
                )
                rating = initial + decay * (rating - initial)
        if previous_roster and roster:
            continuity = sum(player_id in roster for player_id in previous_roster) / 5.0
            rating = initial + continuity**parameters.roster_carry_power * (
                rating - initial
            )
        return rating

    def probability(radiant_rating: float, dire_rating: float) -> float:
        logit = rating_scale * (radiant_rating - dire_rating) + side_logit
        if logit >= 0.0:
            return 1.0 / (1.0 + math.exp(-logit))
        exponential = math.exp(logit)
        return exponential / (1.0 + exponential)

    for row in history:
        radiant_rating = effective(
            row.radiant_team_id,
            row.radiant_roster,
            row.started_at,
        )
        dire_rating = effective(
            row.dire_team_id,
            row.dire_roster,
            row.started_at,
        )
        error = float(row.radiant_win) - probability(radiant_rating, dire_rating)
        states[row.radiant_team_id] = (
            radiant_rating + parameters.k_factor * error,
            row.radiant_roster,
            row.completed_at,
        )
        states[row.dire_team_id] = (
            dire_rating - parameters.k_factor * error,
            row.dire_roster,
            row.completed_at,
        )
    return probability(
        effective(target.radiant_team_id, target.radiant_roster, target.started_at),
        effective(target.dire_team_id, target.dire_roster, target.started_at),
    )


def _candidate_sort_key(
    parameters: TeamRatingParameters,
    *,
    support: int,
    log_loss: float | None,
    brier_score: float | None,
) -> tuple[float, float, tuple[int, int], str]:
    return (
        math.inf if support == 0 or log_loss is None else log_loss,
        math.inf if support == 0 or brier_score is None else brier_score,
        parameters.complexity_key(),
        _canonical_json(parameters.to_payload()),
    )


def _history_available_from(
    maps: Sequence[RatingMapInput],
) -> tuple[int, ...]:
    target_cutoffs = tuple(row.started_at for row in maps)
    result = []
    for row in maps:
        if row.result_usable_at is None:
            result.append(len(maps))
            continue
        result.append(
            max(
                bisect_left(target_cutoffs, row.result_usable_at),
                bisect_right(target_cutoffs, row.completed_at),
            )
        )
    return tuple(result)


def _effective_modifier_inputs(
    maps: Sequence[RatingMapInput],
) -> tuple[tuple[tuple[float | None, float | None], ...], ...]:
    previous: dict[int, tuple[tuple[int, ...], datetime]] = {}
    result: list[tuple[tuple[float | None, float | None], ...]] = []
    for row in maps:
        sides = []
        for team_id, roster in (
            (row.radiant_team_id, row.radiant_roster),
            (row.dire_team_id, row.dire_roster),
        ):
            prior = previous.get(team_id)
            if prior is None:
                sides.append((None, None))
            else:
                prior_roster, last_observed_at = prior
                inactive_days = (
                    row.started_at - last_observed_at
                ).total_seconds() / 86_400.0
                if inactive_days < 0.0:
                    raise ValueError("rating cutoff cannot precede last_observed_at")
                continuity = (
                    None
                    if not prior_roster or not roster
                    else sum(player_id in roster for player_id in prior_roster) / 5.0
                )
                sides.append((inactive_days, continuity))
            previous[team_id] = (roster, row.completed_at)
        result.append(tuple(sides))
    return tuple(result)


def _vectorized_candidate_probabilities(
    maps: Sequence[RatingMapInput],
    candidates: Sequence[TeamRatingParameters],
    histories: Mapping[int, Sequence[RatingMapInput]],
    *,
    batch_size: int = 8,
) -> np.ndarray:
    """Evaluate exact per-cutoff prefix replays in bounded NumPy batches."""

    if not candidates:
        raise ValueError("Team Rating parameter grid cannot be empty")
    count = len(maps)
    probabilities = np.empty((len(candidates), count), dtype=np.float64)
    if not count:
        return probabilities
    if any(
        row.result_usable_at is not None
        and row.result_usable_at != row.completed_at
        for row in maps
    ):
        for parameter_index, parameters in enumerate(candidates):
            probabilities[parameter_index] = tuple(
                _inner_candidate_probability(
                    histories[row.match_id],
                    row,
                    parameters,
                )
                for row in maps
            )
        return probabilities

    initial = 1_500.0
    team_ids = sorted(
        {
            team_id
            for row in maps
            for team_id in (row.radiant_team_id, row.dire_team_id)
        }
    )
    team_indexes = {team_id: index for index, team_id in enumerate(team_ids)}
    radiant_indexes = tuple(team_indexes[row.radiant_team_id] for row in maps)
    dire_indexes = tuple(team_indexes[row.dire_team_id] for row in maps)
    available_from = _history_available_from(maps)
    modifier_inputs = _effective_modifier_inputs(maps)
    side_logits = np.asarray(
        [
            estimate_radiant_side_logit(
                sum(row.radiant_win for row in histories[target.match_id]),
                len(histories[target.match_id]),
            )
            for target in maps
        ],
        dtype=np.float64,
    )

    for batch_start in range(0, len(candidates), batch_size):
        batch = candidates[batch_start : batch_start + batch_size]
        size = len(batch)
        rating_scales = np.asarray(
            [math.log(10.0) / parameters.scale for parameters in batch],
            dtype=np.float64,
        )
        k_factors = np.asarray(
            [parameters.k_factor for parameters in batch],
            dtype=np.float64,
        )
        modifiers = np.ones((size, count, 2), dtype=np.float64)
        for parameter_index, parameters in enumerate(batch):
            for map_index, sides in enumerate(modifier_inputs):
                for side_index, (inactive_days, continuity) in enumerate(sides):
                    if (
                        inactive_days is not None
                        and parameters.inactivity_half_life_days is not None
                    ):
                        modifiers[parameter_index, map_index, side_index] *= math.exp(
                            -math.log(2.0)
                            * inactive_days
                            / parameters.inactivity_half_life_days
                        )
                    if continuity is not None:
                        modifiers[parameter_index, map_index, side_index] *= (
                            continuity**parameters.roster_carry_power
                        )

        ratings = np.full(
            (size, count, len(team_ids)),
            initial,
            dtype=np.float64,
        )
        for map_index, row in enumerate(maps):
            radiant_index = radiant_indexes[map_index]
            dire_index = dire_indexes[map_index]
            radiant_target = initial + modifiers[:, map_index, 0] * (
                ratings[:, map_index, radiant_index] - initial
            )
            dire_target = initial + modifiers[:, map_index, 1] * (
                ratings[:, map_index, dire_index] - initial
            )
            probabilities[
                batch_start : batch_start + size, map_index
            ] = expit(
                rating_scales * (radiant_target - dire_target)
                + side_logits[map_index]
            )

            first_target = available_from[map_index]
            if first_target >= count:
                continue
            radiant_effective = initial + modifiers[:, map_index, 0, None] * (
                ratings[:, first_target:, radiant_index] - initial
            )
            dire_effective = initial + modifiers[:, map_index, 1, None] * (
                ratings[:, first_target:, dire_index] - initial
            )
            expected = expit(
                rating_scales[:, None]
                * (radiant_effective - dire_effective)
                + side_logits[None, first_target:]
            )
            error = float(row.radiant_win) - expected
            ratings[:, first_target:, radiant_index] = (
                radiant_effective + k_factors[:, None] * error
            )
            ratings[:, first_target:, dire_index] = (
                dire_effective - k_factors[:, None] * error
            )
    return probabilities


def _walk_forward_parameter_selections(
    maps: Sequence[RatingMapInput],
    candidates: Sequence[TeamRatingParameters],
    probabilities: np.ndarray,
) -> dict[int, ParameterSelection]:
    count = len(maps)
    if probabilities.shape != (len(candidates), count):
        raise ValueError("candidate probability matrix has invalid dimensions")
    available_from = _history_available_from(maps)
    outcomes = np.asarray([float(row.radiant_win) for row in maps])
    bounded = np.clip(probabilities, 1e-15, 1.0 - 1e-15)
    losses = -(outcomes[None, :] * np.log(bounded) + (1.0 - outcomes)[None, :] * np.log1p(-bounded))
    briers = np.square(probabilities - outcomes[None, :])
    loss_events = np.zeros((len(candidates), count + 1), dtype=np.float64)
    brier_events = np.zeros((len(candidates), count + 1), dtype=np.float64)
    support_events = np.zeros(count + 1, dtype=np.int64)
    for map_index, first_target in enumerate(available_from):
        if first_target < count:
            loss_events[:, first_target] += losses[:, map_index]
            brier_events[:, first_target] += briers[:, map_index]
            support_events[first_target] += 1
    cumulative_loss = np.cumsum(loss_events, axis=1)
    cumulative_brier = np.cumsum(brier_events, axis=1)
    cumulative_support = np.cumsum(support_events)

    selections: dict[int, ParameterSelection] = {}
    for map_index, target in enumerate(maps):
        support = int(cumulative_support[map_index])
        ranked = []
        for parameter_index, parameters in enumerate(candidates):
            log_loss = (
                None
                if support == 0
                else float(cumulative_loss[parameter_index, map_index] / support)
            )
            brier_score = (
                None
                if support == 0
                else float(cumulative_brier[parameter_index, map_index] / support)
            )
            selection = ParameterSelection(
                parameters=parameters,
                support=support,
                log_loss=log_loss,
                brier_score=brier_score,
            )
            ranked.append(
                (
                    _candidate_sort_key(
                        parameters,
                        support=support,
                        log_loss=log_loss,
                        brier_score=brier_score,
                    ),
                    selection,
                )
            )
        selections[target.match_id] = min(ranked, key=lambda value: value[0])[1]
    return selections


def select_team_rating_parameters(
    inner_targets: Sequence[RatingMapInput],
    *,
    candidate_probabilities: Mapping[
        tuple[TeamRatingParameters, int], float
    ],
    candidates: Sequence[TeamRatingParameters] = TEAM_RATING_PARAMETER_GRID,
) -> ParameterSelection:
    if not candidates:
        raise ValueError("Team Rating parameter grid cannot be empty")
    ranked: list[
        tuple[
            tuple[float, float, tuple[int, int], str],
            ParameterSelection,
        ]
    ] = []
    for parameters in candidates:
        probabilities = tuple(
            candidate_probabilities[(parameters, target.match_id)]
            for target in inner_targets
        )
        support = len(probabilities)
        log_loss = (
            None
            if not probabilities
            else math.fsum(
                _log_loss(target.radiant_win, probability)
                for target, probability in zip(inner_targets, probabilities, strict=True)
            )
            / support
        )
        brier_score = (
            None
            if not probabilities
            else math.fsum(
                (probability - float(target.radiant_win)) ** 2
                for target, probability in zip(inner_targets, probabilities, strict=True)
            )
            / support
        )
        selection = ParameterSelection(
            parameters=parameters,
            support=support,
            log_loss=log_loss,
            brier_score=brier_score,
        )
        ranked.append(
            (
                _candidate_sort_key(
                    parameters,
                    support=support,
                    log_loss=log_loss,
                    brier_score=brier_score,
                ),
                selection,
            )
        )
    return min(ranked, key=lambda value: value[0])[1]


def build_team_rating_walk_forward_runs(
    corpus: TeamRatingCorpus,
    *,
    candidates: Sequence[TeamRatingParameters] = TEAM_RATING_PARAMETER_GRID,
) -> tuple[TeamRatingWalkForwardRun, ...]:
    """Run nested chronological parameter selection for every legal target."""

    mode = AvailabilityMode(corpus.availability_mode)
    maps = tuple(loaded.row for loaded in corpus.maps)
    targets = corpus.targets
    sources_by_match = {
        loaded.row.match_id: loaded.source_authority for loaded in corpus.maps
    }
    histories = {
        loaded.row.match_id: _earlier_training_corpus(maps, loaded.row.started_at)
        for loaded in corpus.maps
    }
    candidate_probabilities = _vectorized_candidate_probabilities(
        maps,
        candidates,
        histories,
    )
    selections = _walk_forward_parameter_selections(
        maps,
        candidates,
        candidate_probabilities,
    )

    runs: list[TeamRatingWalkForwardRun] = []
    for loaded in targets:
        target = loaded.row
        assert loaded.prediction_cutoff is not None
        assert loaded.cutoff_source is not None
        history = _earlier_training_corpus(maps, loaded.prediction_cutoff)
        selection = selections[target.match_id]
        config = _config(selection.parameters, history)
        artifact = build_team_rating_artifact(
            history,
            target=target,
            prediction_cutoff=loaded.prediction_cutoff,
            training_cutoff=loaded.prediction_cutoff,
            config=config,
        )
        target_source = sources_by_match[target.match_id]
        ordered_training_sources = tuple(
            sources_by_match[row.match_id] for row in artifact.ordered_training_corpus
        )
        authority_fingerprint = team_rating_authority_fingerprint(
            target_source=target_source,
            ordered_training_sources=ordered_training_sources,
        )
        combined_training_input_hash = combined_team_rating_training_input_hash(
            artifact_training_input_hash=artifact.training_input_hash,
            authority_fingerprint=authority_fingerprint,
        )
        run_id = team_rating_run_id(
            availability_mode=mode,
            artifact_hash=artifact.artifact_hash,
            authority_fingerprint=authority_fingerprint,
        )
        runs.append(
            TeamRatingWalkForwardRun(
                run_id=run_id,
                availability_mode=mode.value,
                cutoff_source=loaded.cutoff_source,
                selection=selection,
                config=config,
                artifact=artifact,
                target_source_authority=target_source,
                ordered_training_sources=ordered_training_sources,
                authority_fingerprint=authority_fingerprint,
                combined_training_input_hash=combined_training_input_hash,
                series_id=target.series_id,
                event_id=target.event_id,
                eventual_radiant_win=target.radiant_win,
                radiant_prior_probability=_radiant_prior(history),
                status=(
                    "trained" if selection.support > 0 else "insufficient_evidence"
                ),
            )
        )
    return tuple(runs)


def _metric_value(metrics: BinaryMetrics, name: str) -> float | None:
    return {
        "brier_score": metrics.brier_score,
        "log_loss": metrics.log_loss,
        "ece": metrics.expected_calibration_error,
        "auc": metrics.auc,
        "accuracy": metrics.accuracy,
    }[name]


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _interval(values: Sequence[float], confidence: float) -> BootstrapInterval:
    tail = (1.0 - confidence) / 2.0
    return BootstrapInterval(
        _percentile(values, tail),
        _percentile(values, 1.0 - tail),
    )


def _cluster_samples(
    points: Sequence[_EvaluationPoint],
    *,
    seed_material: str,
    samples: int,
) -> tuple[tuple[_EvaluationPoint, ...], ...]:
    if samples < 1:
        raise ValueError("bootstrap sample count must be positive")
    if not points:
        return ()
    clusters: dict[str, list[_EvaluationPoint]] = {}
    for point in points:
        key = (
            f"series:{point.series_id}"
            if point.series_id is not None
            else f"match:{point.match_id}"
        )
        clusters.setdefault(key, []).append(point)
    keys = sorted(clusters)
    seed = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
    generator = random.Random(seed)
    return tuple(
        tuple(
            point
            for _ in keys
            for point in clusters[keys[generator.randrange(len(keys))]]
        )
        for _sample in range(samples)
    )


def _probabilities(
    points: Sequence[_EvaluationPoint], model_name: str
) -> tuple[float, ...]:
    return tuple(float(getattr(point, model_name)) for point in points)


def _metrics(
    points: Sequence[_EvaluationPoint], model_name: str
) -> BinaryMetrics:
    return evaluate_binary_predictions(
        (point.outcome for point in points),
        _probabilities(points, model_name),
        ece_bins=CALIBRATION_BINS,
    )


def _model_report(
    points: Sequence[_EvaluationPoint],
    samples: Sequence[Sequence[_EvaluationPoint]],
    model_name: str,
) -> BaselineReport:
    base = _metrics(points, model_name)
    estimates: dict[str, list[float]] = {
        name: []
        for name in ("brier_score", "log_loss", "ece", "auc", "accuracy")
    }
    for sample in samples:
        metrics = _metrics(sample, model_name)
        for name in estimates:
            value = _metric_value(metrics, name)
            if value is not None:
                estimates[name].append(value)
    intervals = MetricIntervals(
        brier_score_90=_interval(estimates["brier_score"], 0.90),
        brier_score_95=_interval(estimates["brier_score"], 0.95),
        log_loss_90=_interval(estimates["log_loss"], 0.90),
        log_loss_95=_interval(estimates["log_loss"], 0.95),
        ece_90=_interval(estimates["ece"], 0.90),
        ece_95=_interval(estimates["ece"], 0.95),
        auc_90=_interval(estimates["auc"], 0.90),
        auc_95=_interval(estimates["auc"], 0.95),
        accuracy_90=_interval(estimates["accuracy"], 0.90),
        accuracy_95=_interval(estimates["accuracy"], 0.95),
    )
    return BaselineReport(
        model_name=model_name,
        support=base.support,
        brier_score=base.brier_score,
        log_loss=base.log_loss,
        ece=base.expected_calibration_error,
        auc=base.auc,
        accuracy=base.accuracy,
        intervals=intervals,
    )


def _paired_delta(
    points: Sequence[_EvaluationPoint],
    samples: Sequence[Sequence[_EvaluationPoint]],
    *,
    baseline: str,
    metric: str,
) -> PairedDeltaReport:
    def loss(point: _EvaluationPoint, model: str) -> float:
        probability = float(getattr(point, model))
        if metric == "brier_score":
            return (probability - float(point.outcome)) ** 2
        if metric == "log_loss":
            return _log_loss(point.outcome, probability)
        raise ValueError(f"unsupported paired metric: {metric}")

    def estimate(rows: Sequence[_EvaluationPoint]) -> float | None:
        if not rows:
            return None
        return math.fsum(
            loss(point, "team_rating") - loss(point, baseline) for point in rows
        ) / len(rows)

    estimates = tuple(
        value
        for sample in samples
        if (value := estimate(sample)) is not None
    )
    return PairedDeltaReport(
        comparison=f"team_rating-{baseline}",
        metric=metric,
        delta=estimate(points),
        ci_90=_interval(estimates, 0.90),
        ci_95=_interval(estimates, 0.95),
        probability_of_improvement=(
            None
            if not estimates
            else sum(value < 0.0 for value in estimates) / len(estimates)
        ),
    )


def evaluate_team_rating_runs(
    runs: Sequence[TeamRatingWalkForwardRun],
    *,
    bootstrap_samples: int = BOOTSTRAP_SAMPLES,
) -> tuple[
    tuple[BaselineReport, ...],
    tuple[PairedDeltaReport, ...],
    GateReport,
]:
    allowed_statuses = {"trained", "insufficient_evidence", "failed"}
    invalid_statuses = sorted(
        {run.status for run in runs if run.status not in allowed_statuses}
    )
    if invalid_statuses:
        raise ValueError(
            "unsupported Team Rating run statuses: " + ", ".join(invalid_statuses)
        )
    failed_targets = sum(run.status == "failed" for run in runs)
    evaluated_runs = tuple(run for run in runs if run.status == "trained")
    points = tuple(
        _EvaluationPoint(
            match_id=run.artifact.target.match_id,
            series_id=run.series_id,
            event_id=run.event_id,
            outcome=run.eventual_radiant_win,
            constant_50=0.5,
            radiant_prior=run.radiant_prior_probability,
            team_rating=run.artifact.prediction.raw_probability,
        )
        for run in evaluated_runs
    )
    if not points:
        empty_interval = BootstrapInterval(None, None)
        empty_intervals = MetricIntervals(*(empty_interval,) * 10)
        baselines = tuple(
            BaselineReport(name, 0, None, None, None, None, None, empty_intervals)
            for name in ("constant_50", "radiant_prior", "team_rating")
        )
        if failed_targets:
            return (
                baselines,
                (),
                GateReport("failed", (f"failed_targets={failed_targets}",)),
            )
        return baselines, (), GateReport("unsupported", ("support=0",))
    samples = _cluster_samples(
        points,
        seed_material=BOOTSTRAP_SEED_MATERIAL,
        samples=bootstrap_samples,
    )
    baselines = tuple(
        _model_report(points, samples, name)
        for name in ("constant_50", "radiant_prior", "team_rating")
    )
    deltas = tuple(
        _paired_delta(points, samples, baseline=baseline, metric=metric)
        for baseline in ("constant_50", "radiant_prior")
        for metric in ("log_loss", "brier_score")
    )
    failures: list[str] = []
    if failed_targets:
        failures.append(f"failed_targets={failed_targets}")
    for baseline in ("constant_50", "radiant_prior"):
        comparisons = tuple(
            row for row in deltas if row.comparison == f"team_rating-{baseline}"
        )
        stable = any(
            row.delta is not None
            and row.delta < 0.0
            and row.ci_90.upper is not None
            and row.ci_90.upper < 0.0
            for row in comparisons
        )
        if not stable:
            failures.append(f"no_stable_improvement_vs_{baseline}")
    return (
        baselines,
        deltas,
        GateReport("passed" if not failures else "failed", tuple(failures)),
    )


def build_team_rating_report(
    corpus: TeamRatingCorpus,
    runs: Sequence[TeamRatingWalkForwardRun],
    *,
    dry_run: bool,
    inserted_runs: int = 0,
    unchanged_runs: int = 0,
    inserted_predictions: int = 0,
    unchanged_predictions: int = 0,
    inserted_snapshots: int = 0,
    unchanged_snapshots: int = 0,
    bootstrap_samples: int = BOOTSTRAP_SAMPLES,
) -> TeamRatingBacktestReport:
    baselines, deltas, gate = evaluate_team_rating_runs(
        runs,
        bootstrap_samples=bootstrap_samples,
    )
    selected_counts: dict[str, int] = {}
    for run in runs:
        key = _canonical_json(run.selection.parameters.to_payload())
        selected_counts[key] = selected_counts.get(key, 0) + 1
    starts = tuple(row.row.started_at for row in corpus.maps)
    completions = tuple(row.row.completed_at for row in corpus.maps)
    eligible_targets = len(corpus.targets)
    evaluated_targets = sum(run.status == "trained" for run in runs)
    insufficient_targets = sum(
        run.status == "insufficient_evidence" for run in runs
    )
    failed_targets = sum(run.status == "failed" for run in runs)
    return TeamRatingBacktestReport(
        backtest_version=TEAM_RATING_BACKTEST_VERSION,
        bootstrap_algorithm=BOOTSTRAP_ALGORITHM_VERSION,
        bootstrap_seed_material=BOOTSTRAP_SEED_MATERIAL,
        bootstrap_samples=bootstrap_samples,
        availability_mode=corpus.availability_mode,
        dry_run=dry_run,
        formal_maps=corpus.formal_maps,
        eligible_targets=eligible_targets,
        evaluated_targets=evaluated_targets,
        insufficient_targets=insufficient_targets,
        failed_targets=failed_targets,
        evaluation_coverage=(
            evaluated_targets / eligible_targets if eligible_targets else 0.0
        ),
        inserted_runs=inserted_runs,
        unchanged_runs=unchanged_runs,
        inserted_predictions=inserted_predictions,
        unchanged_predictions=unchanged_predictions,
        inserted_snapshots=inserted_snapshots,
        unchanged_snapshots=unchanged_snapshots,
        corpus_started_at=min(starts).isoformat() if starts else None,
        corpus_completed_at=max(completions).isoformat() if completions else None,
        selected_parameter_counts=tuple(sorted(selected_counts.items())),
        baselines=baselines,
        paired_deltas=deltas,
        gate=gate,
    )


def report_as_dict(report: TeamRatingBacktestReport) -> dict[str, Any]:
    return asdict(report)


def report_as_markdown(report: TeamRatingBacktestReport) -> str:
    lines = [
        "# Team Rating Walk-forward Report",
        "",
        f"- Backtest: `{report.backtest_version}`",
        f"- Bootstrap: `{report.bootstrap_algorithm}`",
        f"- Bootstrap seed: `{report.bootstrap_seed_material}`",
        f"- Bootstrap samples: {report.bootstrap_samples}",
        f"- Availability mode: `{report.availability_mode}`",
        f"- Formal maps: {report.formal_maps}",
        f"- Eligible targets: {report.eligible_targets}",
        f"- Evaluated targets: {report.evaluated_targets}",
        f"- Insufficient targets: {report.insufficient_targets}",
        f"- Failed targets: {report.failed_targets}",
        f"- Evaluation coverage: {report.evaluation_coverage:.6f}",
        f"- Corpus: {report.corpus_started_at} to {report.corpus_completed_at}",
        f"- Gate: `{report.gate.status}`",
        "",
        "| Model | Support | Brier | Log loss | ECE | AUC | Accuracy |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    def render(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.6f}"

    for row in report.baselines:
        lines.append(
            f"| {row.model_name} | {row.support} | {render(row.brier_score)} | "
            f"{render(row.log_loss)} | {render(row.ece)} | {render(row.auc)} | "
            f"{render(row.accuracy)} |"
        )

    lines.extend(
        (
            "",
            "## Baseline 90% Series-cluster Bootstrap Intervals",
            "",
            "| Model | Brier | Log loss | ECE | AUC | Accuracy |",
            "| --- | --- | --- | --- | --- | --- |",
        )
    )
    for row in report.baselines:
        lines.append(
            f"| {row.model_name} | "
            f"[{render(row.intervals.brier_score_90.lower)}, "
            f"{render(row.intervals.brier_score_90.upper)}] | "
            f"[{render(row.intervals.log_loss_90.lower)}, "
            f"{render(row.intervals.log_loss_90.upper)}] | "
            f"[{render(row.intervals.ece_90.lower)}, "
            f"{render(row.intervals.ece_90.upper)}] | "
            f"[{render(row.intervals.auc_90.lower)}, "
            f"{render(row.intervals.auc_90.upper)}] | "
            f"[{render(row.intervals.accuracy_90.lower)}, "
            f"{render(row.intervals.accuracy_90.upper)}] |"
        )
    lines.extend(("", "## Paired Series-cluster Bootstrap", ""))
    for row in report.paired_deltas:
        lines.append(
            f"- `{row.comparison}` {row.metric}: delta={render(row.delta)}, "
            f"90% CI=[{render(row.ci_90.lower)}, {render(row.ci_90.upper)}], "
            f"95% CI=[{render(row.ci_95.lower)}, {render(row.ci_95.upper)}], "
            f"P(improvement)={render(row.probability_of_improvement)}"
        )
    if report.gate.failures:
        lines.extend(("", "## Gate Failures", ""))
        lines.extend(f"- `{failure}`" for failure in report.gate.failures)
    return "\n".join(lines) + "\n"


def run_team_rating_backtest(
    storage: object,
    *,
    availability_mode: AvailabilityMode = AvailabilityMode.RECONSTRUCTED,
    dry_run: bool = False,
    checkpoint_latest: bool = False,
    candidates: Sequence[TeamRatingParameters] = TEAM_RATING_PARAMETER_GRID,
    max_maps: int | None = None,
) -> TeamRatingBacktestReport:
    """Load, evaluate, and atomically persist formal Team Rating runs."""

    from .storage import IntelligenceStorage
    from .team_rating_storage import persist_team_rating_runs

    if not isinstance(storage, IntelligenceStorage):
        raise ValueError("storage must be an IntelligenceStorage")
    if not isinstance(availability_mode, AvailabilityMode):
        raise ValueError("availability_mode must be an AvailabilityMode")
    if availability_mode is not AvailabilityMode.RECONSTRUCTED:
        raise ValueError(
            "formal Team Rating backtest only supports reconstructed walk-forward"
        )
    if max_maps is not None and (
        isinstance(max_maps, bool) or not isinstance(max_maps, int) or max_maps < 1
    ):
        raise ValueError("max_maps must be a positive integer")
    with storage.connection.transaction():
        corpus = load_team_rating_corpus(
            storage.connection,
            availability_mode=availability_mode,
        )
    if max_maps is not None:
        selected_maps = corpus.maps[:max_maps]
        corpus = TeamRatingCorpus(
            corpus.availability_mode,
            len(selected_maps),
            selected_maps,
        )
    runs = build_team_rating_walk_forward_runs(corpus, candidates=candidates)
    checkpoint_run_ids = (runs[-1].run_id,) if checkpoint_latest and runs else ()
    counts = persist_team_rating_runs(
        storage.connection,
        runs,
        dry_run=dry_run,
        checkpoint_run_ids=checkpoint_run_ids,
    )
    return build_team_rating_report(
        corpus,
        runs,
        dry_run=dry_run,
        inserted_runs=counts.inserted_runs,
        unchanged_runs=counts.unchanged_runs,
        inserted_predictions=counts.inserted_predictions,
        unchanged_predictions=counts.unchanged_predictions,
        inserted_snapshots=counts.inserted_snapshots,
        unchanged_snapshots=counts.unchanged_snapshots,
        bootstrap_samples=BOOTSTRAP_SAMPLES,
    )


__all__ = [
    "BOOTSTRAP_SAMPLES",
    "BOOTSTRAP_ALGORITHM_VERSION",
    "BOOTSTRAP_SEED_MATERIAL",
    "TEAM_RATING_BACKTEST_VERSION",
    "TEAM_RATING_PARAMETER_GRID",
    "BaselineReport",
    "BootstrapInterval",
    "GateReport",
    "LoadedTeamRatingMap",
    "MetricIntervals",
    "PairedDeltaReport",
    "ParameterSelection",
    "TeamRatingBacktestReport",
    "TeamRatingCorpus",
    "TeamRatingParameters",
    "TeamRatingSourceAuthority",
    "TeamRatingWalkForwardRun",
    "build_team_rating_report",
    "build_team_rating_walk_forward_runs",
    "evaluate_team_rating_runs",
    "combined_team_rating_training_input_hash",
    "load_team_rating_corpus",
    "report_as_dict",
    "report_as_markdown",
    "run_team_rating_backtest",
    "select_team_rating_parameters",
    "team_rating_authority_fingerprint",
    "team_rating_run_id",
]
