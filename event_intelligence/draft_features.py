"""Causal, source-auditable Dota 2 draft feature snapshots."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from itertools import combinations
from typing import Any, Callable, Iterable, Sequence

from .models import RolePurpose
from .raw_archive import canonical_json_bytes
from .roles import RoleSource


FEATURE_VERSION = "draft-features-v3"
ROLE_CONFIDENCE_MIN = 0.7
MIN_FEATURE_SUPPORT = 5
SMOOTHING_SUPPORT = 2.0


class AvailabilityMode(str, Enum):
    PROSPECTIVE = "prospective"
    RECONSTRUCTED = "reconstructed_walk_forward"


@dataclass(frozen=True)
class DerivedFactProvenance:
    """Availability and immutable identity for one derived fact family."""

    cutoff: datetime
    first_usable_at: datetime | None
    input_hash: str
    version: str

    def __post_init__(self) -> None:
        cutoff = _utc(self.cutoff, "derived fact cutoff")
        first_usable_at = (
            None
            if self.first_usable_at is None
            else _utc(self.first_usable_at, "derived fact first_usable_at")
        )
        if first_usable_at is not None and first_usable_at < cutoff:
            raise ValueError("derived fact first_usable_at cannot precede cutoff")
        object.__setattr__(self, "cutoff", cutoff)
        object.__setattr__(self, "first_usable_at", first_usable_at)
        object.__setattr__(
            self,
            "input_hash",
            _sha256_hash(self.input_hash, "derived fact input_hash"),
        )
        object.__setattr__(self, "version", _scope(self.version, "derived fact version"))


@dataclass(frozen=True)
class ExpectedRoleAssignment:
    """A pre-map role assignment that cannot represent target-map evidence."""

    purpose: RolePurpose
    source: RoleSource
    position: int | None
    confidence: float
    provenance: DerivedFactProvenance

    def __post_init__(self) -> None:
        if self.purpose is not RolePurpose.EXPECTED_POSITION:
            raise ValueError("draft roles must have expected_position purpose")
        if not isinstance(self.source, RoleSource):
            raise ValueError("draft role source must be a RoleSource")
        if self.source is RoleSource.SINGLE_MAP:
            raise ValueError("target-map single_map evidence cannot be an expected role")
        if self.position is not None and self.position not in range(1, 6):
            raise ValueError("expected position must be between 1 and 5 or None")
        _probability(self.confidence, "expected role confidence")
        if self.position is None:
            if self.confidence != 0.0 or self.source is not RoleSource.UNKNOWN:
                raise ValueError("an unknown expected role must use zero confidence")
        elif self.source is RoleSource.UNKNOWN:
            raise ValueError("a known expected role cannot use unknown source")


@dataclass(frozen=True)
class DraftPlayer:
    """A player/hero pair; target players require an expected-role assignment."""

    player_id: int
    hero_id: int
    expected_role: ExpectedRoleAssignment | None = None

    def __post_init__(self) -> None:
        _nonzero_int(self.player_id, "player_id")
        _positive_int(self.hero_id, "hero_id")
        if self.expected_role is not None and not isinstance(
            self.expected_role, ExpectedRoleAssignment
        ):
            raise ValueError("expected_role must be an ExpectedRoleAssignment or None")

    @property
    def expected_position(self) -> int | None:
        return None if self.expected_role is None else self.expected_role.position

    @property
    def expected_position_confidence(self) -> float:
        return 0.0 if self.expected_role is None else self.expected_role.confidence


@dataclass(frozen=True)
class DraftTeam:
    team_id: int
    players: tuple[DraftPlayer, ...]

    def __post_init__(self) -> None:
        _positive_int(self.team_id, "team_id")
        if len(self.players) != 5:
            raise ValueError("a draft team must contain exactly five players")
        player_ids = tuple(player.player_id for player in self.players)
        hero_ids = tuple(player.hero_id for player in self.players)
        if len(set(player_ids)) != 5:
            raise ValueError("draft team player IDs must be unique")
        if len(set(hero_ids)) != 5:
            raise ValueError("draft team hero IDs must be unique")
        known_positions = tuple(
            player.expected_position
            for player in self.players
            if player.expected_position is not None
        )
        if len(known_positions) != len(set(known_positions)):
            raise ValueError("known expected positions must be one-to-one within a team")


@dataclass(frozen=True)
class DraftHeroMapEvidence:
    """Post-map exact facts. These are legal only on earlier completed maps."""

    player_id: int
    hero_id: int
    observed_position: int | None = None
    observed_position_confidence: float = 0.0
    observed_role_purpose: RolePurpose | None = None
    observed_role_source: RoleSource | None = None
    observed_role_provenance: DerivedFactProvenance | None = None
    execution_score: float | None = None
    score_provenance: DerivedFactProvenance | None = None
    control_seconds: float | None = None
    hero_healing: float | None = None
    last_hits: float | None = None
    tower_damage: float | None = None
    net_worth: float | None = None
    buyback_count: int | None = None

    def __post_init__(self) -> None:
        _nonzero_int(self.player_id, "player_id")
        _positive_int(self.hero_id, "hero_id")
        if self.observed_position is not None and self.observed_position not in range(1, 6):
            raise ValueError("observed_position must be between 1 and 5 or None")
        _probability(
            self.observed_position_confidence, "observed_position_confidence"
        )
        if self.observed_position is None:
            if (
                self.observed_position_confidence != 0.0
                or self.observed_role_purpose is not None
                or self.observed_role_source is not None
                or self.observed_role_provenance is not None
            ):
                raise ValueError("observed role provenance requires an observed position")
        else:
            if self.observed_role_purpose is not RolePurpose.OBSERVED_POSITION:
                raise ValueError("observed position must use observed_position purpose")
            if (
                not isinstance(self.observed_role_source, RoleSource)
                or self.observed_role_source is RoleSource.UNKNOWN
            ):
                raise ValueError("known observed position requires a known role source")
            if self.observed_role_provenance is None:
                raise ValueError("observed position requires role provenance")
        if self.execution_score is not None:
            _bounded_number(self.execution_score, "execution_score", 0.0, 100.0)
            if self.score_provenance is None:
                raise ValueError("execution_score requires score provenance")
        elif self.score_provenance is not None:
            raise ValueError("score provenance requires execution_score")
        for name in (
            "control_seconds",
            "hero_healing",
            "last_hits",
            "tower_damage",
            "net_worth",
        ):
            value = getattr(self, name)
            if value is not None:
                _nonnegative_number(value, name)
        if self.buyback_count is not None:
            _nonnegative_int(self.buyback_count, "buyback_count")


@dataclass(frozen=True)
class DraftTeamMapEvidence:
    """Opportunity-conditional team facts from one completed map."""

    comeback_opportunity: bool | None = None
    came_back: bool | None = None
    throw_opportunity: bool | None = None
    threw: bool | None = None
    closeout_opportunity: bool | None = None
    closed_out: bool | None = None
    roshan_events: int | None = None
    high_ground_events: int | None = None
    long_fight_wins: int | None = None
    long_fight_opportunities: int | None = None
    state_provenance: DerivedFactProvenance | None = None

    def __post_init__(self) -> None:
        _opportunity_result(
            self.comeback_opportunity, self.came_back, "comeback"
        )
        _opportunity_result(self.throw_opportunity, self.threw, "throw")
        _opportunity_result(
            self.closeout_opportunity, self.closed_out, "closeout"
        )
        for name in (
            "roshan_events",
            "high_ground_events",
            "long_fight_wins",
            "long_fight_opportunities",
        ):
            value = getattr(self, name)
            if value is not None:
                _nonnegative_int(value, name)
        if (
            self.long_fight_wins is not None
            and self.long_fight_opportunities is not None
            and self.long_fight_wins > self.long_fight_opportunities
        ):
            raise ValueError("long_fight_wins cannot exceed opportunities")
        if (self.long_fight_wins is None) != (
            self.long_fight_opportunities is None
        ):
            raise ValueError("long-fight wins and opportunities must be supplied together")
        facts_present = any(
            getattr(self, name) is not None
            for name in (
                "comeback_opportunity",
                "came_back",
                "throw_opportunity",
                "threw",
                "closeout_opportunity",
                "closed_out",
                "roshan_events",
                "high_ground_events",
                "long_fight_wins",
                "long_fight_opportunities",
            )
        )
        if facts_present and self.state_provenance is None:
            raise ValueError("team-state facts require state provenance")
        if not facts_present and self.state_provenance is not None:
            raise ValueError("state provenance requires team-state facts")


@dataclass(frozen=True)
class DraftStyleRateSnapshot:
    """One style posterior with support specific to that rate."""

    value: float | None
    support: int
    coverage: float

    def __post_init__(self) -> None:
        if self.value is not None:
            _probability(self.value, "style rate value")
        _nonnegative_int(self.support, "style rate support")
        _probability(self.coverage, "style rate coverage")
        if self.value is None and (self.support != 0 or self.coverage != 0.0):
            raise ValueError("an unavailable style rate must have zero support and coverage")


@dataclass(frozen=True)
class DraftStyleSnapshot:
    """Versioned, pre-map TeamStyleProfile values for one target team."""

    team_id: int
    availability_mode: AvailabilityMode
    provenance: DerivedFactProvenance
    comeback_rate: DraftStyleRateSnapshot
    throw_resilience_rate: DraftStyleRateSnapshot
    closeout_rate: DraftStyleRateSnapshot

    def __post_init__(self) -> None:
        _positive_int(self.team_id, "style team_id")
        if not isinstance(self.availability_mode, AvailabilityMode):
            raise ValueError("style availability_mode must be an AvailabilityMode")
        for name in (
            "comeback_rate",
            "throw_resilience_rate",
            "closeout_rate",
        ):
            value = getattr(self, name)
            if not isinstance(value, DraftStyleRateSnapshot):
                raise ValueError(f"{name} must be a DraftStyleRateSnapshot")


@dataclass(frozen=True)
class DraftMapEvidence:
    evidence_id: str
    source_input_hash: str
    match_id: int
    completed_at: datetime
    first_usable_at: datetime | None
    event_id: str
    patch: int | None
    duration_seconds: int
    radiant: DraftTeam
    dire: DraftTeam
    radiant_win: bool
    series_id: int | None = None
    map_number: int | None = None
    radiant_hero_evidence: tuple[DraftHeroMapEvidence, ...] = ()
    dire_hero_evidence: tuple[DraftHeroMapEvidence, ...] = ()
    radiant_team_evidence: DraftTeamMapEvidence = field(
        default_factory=DraftTeamMapEvidence
    )
    dire_team_evidence: DraftTeamMapEvidence = field(
        default_factory=DraftTeamMapEvidence
    )

    def __post_init__(self) -> None:
        _scope(self.evidence_id, "evidence_id")
        object.__setattr__(
            self,
            "source_input_hash",
            _sha256_hash(self.source_input_hash, "source_input_hash"),
        )
        _positive_int(self.match_id, "match_id")
        completed = _utc(self.completed_at, "completed_at")
        usable = (
            None
            if self.first_usable_at is None
            else _utc(self.first_usable_at, "first_usable_at")
        )
        if usable is not None and usable < completed:
            raise ValueError("first_usable_at cannot precede completed_at")
        _scope(self.event_id, "event_id")
        if self.patch is not None:
            _positive_int(self.patch, "patch")
        _positive_int(self.duration_seconds, "duration_seconds")
        if not isinstance(self.radiant_win, bool):
            raise ValueError("radiant_win must be boolean")
        if self.radiant.team_id == self.dire.team_id:
            raise ValueError("radiant and dire team IDs must differ")
        heroes = tuple(
            player.hero_id for team in (self.radiant, self.dire) for player in team.players
        )
        if len(set(heroes)) != 10:
            raise ValueError("a map draft must contain ten unique heroes")
        if any(
            player.expected_role is not None
            for team in (self.radiant, self.dire)
            for player in team.players
        ):
            raise ValueError("historical map lineups cannot carry expected roles")
        if self.series_id is not None:
            _positive_int(self.series_id, "series_id")
        if self.map_number is not None:
            _positive_int(self.map_number, "map_number")
        _validate_hero_evidence(
            self.radiant, self.radiant_hero_evidence, "radiant_hero_evidence"
        )
        _validate_hero_evidence(
            self.dire, self.dire_hero_evidence, "dire_hero_evidence"
        )
        derived = tuple(
            provenance
            for fact in (*self.radiant_hero_evidence, *self.dire_hero_evidence)
            for provenance in (
                fact.observed_role_provenance,
                fact.score_provenance,
            )
            if provenance is not None
        ) + tuple(
            provenance
            for provenance in (
                self.radiant_team_evidence.state_provenance,
                self.dire_team_evidence.state_provenance,
            )
            if provenance is not None
        )
        for provenance in derived:
            derived_usable = provenance.first_usable_at
            if derived_usable is not None and _utc(
                derived_usable, "derived fact first_usable_at"
            ) < completed:
                raise ValueError("derived fact availability cannot precede completion")
            if (
                usable is not None
                and derived_usable is not None
                and _utc(derived_usable, "derived fact first_usable_at") < usable
            ):
                raise ValueError("derived fact availability cannot precede map availability")


@dataclass(frozen=True)
class DraftTarget:
    """Prediction-time inputs. Target outcome and observed facts do not exist here."""

    match_id: int
    prediction_cutoff: datetime
    event_id: str
    patch: int | None
    radiant: DraftTeam
    dire: DraftTeam
    availability_mode: AvailabilityMode = AvailabilityMode.PROSPECTIVE
    series_id: int | None = None
    map_number: int | None = None
    radiant_style: DraftStyleSnapshot | None = None
    dire_style: DraftStyleSnapshot | None = None

    def __post_init__(self) -> None:
        _positive_int(self.match_id, "match_id")
        _utc(self.prediction_cutoff, "prediction_cutoff")
        _scope(self.event_id, "event_id")
        if self.patch is not None:
            _positive_int(self.patch, "patch")
        if self.radiant.team_id == self.dire.team_id:
            raise ValueError("radiant and dire team IDs must differ")
        heroes = tuple(
            player.hero_id for team in (self.radiant, self.dire) for player in team.players
        )
        if len(set(heroes)) != 10:
            raise ValueError("a target draft must contain ten unique heroes")
        if not isinstance(self.availability_mode, AvailabilityMode):
            raise ValueError("availability_mode must be an AvailabilityMode")
        cutoff = _utc(self.prediction_cutoff, "prediction_cutoff")
        for player in (*self.radiant.players, *self.dire.players):
            role = player.expected_role
            if role is None:
                raise ValueError("every target player requires an expected-role record")
            if not _provenance_available(
                role.provenance, cutoff, self.availability_mode
            ):
                raise ValueError("expected role was not available at prediction cutoff")
        for side, team, style in (
            ("radiant", self.radiant, self.radiant_style),
            ("dire", self.dire, self.dire_style),
        ):
            if style is None:
                continue
            if style.team_id != team.team_id:
                raise ValueError(f"{side} style team_id does not match target team")
            if style.availability_mode is not self.availability_mode:
                raise ValueError(f"{side} style availability mode does not match target")
            if not _provenance_available(
                style.provenance, cutoff, self.availability_mode
            ):
                raise ValueError(f"{side} style was not available at prediction cutoff")
        if self.series_id is not None:
            _positive_int(self.series_id, "series_id")
        if self.map_number is not None:
            _positive_int(self.map_number, "map_number")


@dataclass(frozen=True)
class FeatureEstimate:
    name: str
    value: float | None
    support: int
    evidence_ids: tuple[str, ...]
    coverage: float
    missing_reason: str | None = None


@dataclass(frozen=True)
class DraftFeatureSnapshot:
    match_id: int
    prediction_cutoff: datetime
    availability_mode: AvailabilityMode
    feature_version: str
    feature_schema: tuple[str, ...]
    feature_schema_hash: str
    input_hash: str
    pure_features: tuple[FeatureEstimate, ...]
    context_features: tuple[FeatureEstimate, ...]
    evidence_ids: tuple[str, ...]
    support: int
    pure_coverage: float
    context_coverage: float
    coverage: float

    def feature(self, name: str) -> FeatureEstimate:
        for value in (*self.pure_features, *self.context_features):
            if value.name == name:
                return value
        raise KeyError(name)

    def pure_values(self) -> dict[str, float | None]:
        return {row.name: row.value for row in self.pure_features}

    def context_values(self, *, include_pure: bool = True) -> dict[str, float | None]:
        rows = (
            (*self.pure_features, *self.context_features)
            if include_pure
            else self.context_features
        )
        return {row.name: row.value for row in rows}


PURE_FEATURE_SCHEMA = (
    "hero_win_rate_diff",
    "role_fit_win_rate_diff",
    "synergy_win_rate_diff",
    "counter_win_rate_edge",
    "scaling_40m_win_rate_diff",
    "control_initiation_proxy_diff",
    "save_sustain_proxy_diff",
    "wave_clear_proxy_diff",
    "push_high_ground_proxy_diff",
    "roshan_proxy_diff",
    "farm_demand_balance_diff",
    "long_fight_buyback_proxy_diff",
    "mobility_global_split_diff",
    "damage_profile_diff",
)

CONTEXT_FEATURE_SCHEMA = (
    "context_comeback_rate_diff",
    "context_throw_resilience_diff",
    "context_closeout_rate_diff",
    "context_player_form_diff",
    "context_roster_stability_diff",
    "context_patch_adaptation_diff",
    "context_opponent_strength_diff",
    "context_series_prior_win_diff",
)

FEATURE_SCHEMA = PURE_FEATURE_SCHEMA + CONTEXT_FEATURE_SCHEMA
FEATURE_SCHEMA_HASH = hashlib.sha256(
    canonical_json_bytes(
        {
            "version": FEATURE_VERSION,
            "features": [
                {
                    "name": name,
                    "group": "pure" if name in PURE_FEATURE_SCHEMA else "context",
                    "missing_values": "explicit_null_with_coverage",
                }
                for name in FEATURE_SCHEMA
            ],
        }
    )
).hexdigest()


@dataclass(frozen=True)
class _SideRow:
    evidence_id: str
    match: DraftMapEvidence
    team: DraftTeam
    opponent: DraftTeam
    won: bool
    heroes: tuple[DraftHeroMapEvidence, ...]
    facts: DraftTeamMapEvidence

    def hero_fact(self, hero_id: int) -> DraftHeroMapEvidence | None:
        return next((row for row in self.heroes if row.hero_id == hero_id), None)


@dataclass(frozen=True)
class _Count:
    successes: int
    opportunities: int
    evidence_ids: tuple[str, ...]


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _scope(value: Any, field: str) -> str:
    result = str(value).strip()
    if not result:
        raise ValueError(f"{field} must be non-empty")
    return result


def _sha256_hash(value: Any, field: str) -> str:
    result = _scope(value, field)
    if len(result) != 64:
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    try:
        int(result, 16)
    except ValueError as error:
        raise ValueError(f"{field} must be a SHA-256 hex digest") from error
    return result.lower()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonzero_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        raise ValueError(f"{field} must be a non-zero integer")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _bounded_number(value: Any, field: str, low: float, high: float) -> float:
    if not _finite(value) or not low <= float(value) <= high:
        raise ValueError(f"{field} must be finite and between {low} and {high}")
    return float(value)


def _nonnegative_number(value: Any, field: str) -> float:
    if not _finite(value) or float(value) < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return float(value)


def _probability(value: Any, field: str) -> float:
    return _bounded_number(value, field, 0.0, 1.0)


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _provenance_available(
    provenance: DerivedFactProvenance,
    cutoff: datetime,
    mode: AvailabilityMode,
) -> bool:
    if _utc(provenance.cutoff, "derived fact cutoff") > cutoff:
        return False
    if mode is AvailabilityMode.RECONSTRUCTED:
        return True
    return (
        provenance.first_usable_at is not None
        and _utc(provenance.first_usable_at, "derived fact first_usable_at") <= cutoff
    )


def _opportunity_result(
    opportunity: bool | None, result: bool | None, field: str
) -> None:
    if opportunity is not None and not isinstance(opportunity, bool):
        raise ValueError(f"{field}_opportunity must be boolean or None")
    if result is not None and not isinstance(result, bool):
        raise ValueError(f"{field} result must be boolean or None")
    if opportunity is True and result is None:
        raise ValueError(f"{field} result is required for a known opportunity")
    if opportunity is not True and result is not None:
        raise ValueError(f"{field} result requires an opportunity")


def _validate_hero_evidence(
    team: DraftTeam,
    evidence: Sequence[DraftHeroMapEvidence],
    field: str,
) -> None:
    if len({row.player_id for row in evidence}) != len(evidence):
        raise ValueError(f"{field} player IDs must be unique")
    if len({row.hero_id for row in evidence}) != len(evidence):
        raise ValueError(f"{field} hero IDs must be unique")
    lineup = {(row.player_id, row.hero_id) for row in team.players}
    if any((row.player_id, row.hero_id) not in lineup for row in evidence):
        raise ValueError(f"{field} must match the team's player/hero pairs")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc(value, "datetime").isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _canonical_team(team: DraftTeam) -> dict[str, Any]:
    return {
        "team_id": team.team_id,
        "players": [
            _jsonable(player)
            for player in sorted(team.players, key=lambda row: (row.player_id, row.hero_id))
        ],
    }


def _canonical_evidence(value: DraftMapEvidence) -> dict[str, Any]:
    return {
        "evidence_id": value.evidence_id,
        "source_input_hash": value.source_input_hash,
        "match_id": value.match_id,
        "completed_at": _utc(value.completed_at, "completed_at").isoformat(),
        "first_usable_at": (
            None
            if value.first_usable_at is None
            else _utc(value.first_usable_at, "first_usable_at").isoformat()
        ),
        "event_id": value.event_id,
        "patch": value.patch,
        "duration_seconds": value.duration_seconds,
        "series_id": value.series_id,
        "map_number": value.map_number,
        "radiant": _canonical_team(value.radiant),
        "dire": _canonical_team(value.dire),
        "radiant_win": value.radiant_win,
        "radiant_hero_evidence": [
            _jsonable(row)
            for row in sorted(
                value.radiant_hero_evidence,
                key=lambda row: (row.player_id, row.hero_id),
            )
        ],
        "dire_hero_evidence": [
            _jsonable(row)
            for row in sorted(
                value.dire_hero_evidence,
                key=lambda row: (row.player_id, row.hero_id),
            )
        ],
        "radiant_team_evidence": _jsonable(value.radiant_team_evidence),
        "dire_team_evidence": _jsonable(value.dire_team_evidence),
    }


def _canonical_target(target: DraftTarget) -> dict[str, Any]:
    return {
        "match_id": target.match_id,
        "prediction_cutoff": _utc(
            target.prediction_cutoff, "prediction_cutoff"
        ).isoformat(),
        "event_id": target.event_id,
        "patch": target.patch,
        "series_id": target.series_id,
        "map_number": target.map_number,
        "radiant": _canonical_team(target.radiant),
        "dire": _canonical_team(target.dire),
        "availability_mode": target.availability_mode.value,
        "radiant_style": _jsonable(target.radiant_style),
        "dire_style": _jsonable(target.dire_style),
    }


def _causal_hero_fact(
    fact: DraftHeroMapEvidence,
    cutoff: datetime,
    mode: AvailabilityMode,
) -> DraftHeroMapEvidence:
    result = fact
    role_provenance = fact.observed_role_provenance
    if role_provenance is not None and not _provenance_available(
        role_provenance, cutoff, mode
    ):
        result = replace(
            result,
            observed_position=None,
            observed_position_confidence=0.0,
            observed_role_purpose=None,
            observed_role_source=None,
            observed_role_provenance=None,
        )
    score_provenance = fact.score_provenance
    if score_provenance is not None and not _provenance_available(
        score_provenance, cutoff, mode
    ):
        result = replace(result, execution_score=None, score_provenance=None)
    return result


def _causal_team_fact(
    fact: DraftTeamMapEvidence,
    cutoff: datetime,
    mode: AvailabilityMode,
) -> DraftTeamMapEvidence:
    provenance = fact.state_provenance
    if provenance is None or _provenance_available(provenance, cutoff, mode):
        return fact
    return DraftTeamMapEvidence()


def _causal_history_row(
    row: DraftMapEvidence,
    cutoff: datetime,
    mode: AvailabilityMode,
) -> DraftMapEvidence:
    return replace(
        row,
        radiant_hero_evidence=tuple(
            _causal_hero_fact(fact, cutoff, mode)
            for fact in row.radiant_hero_evidence
        ),
        dire_hero_evidence=tuple(
            _causal_hero_fact(fact, cutoff, mode)
            for fact in row.dire_hero_evidence
        ),
        radiant_team_evidence=_causal_team_fact(
            row.radiant_team_evidence, cutoff, mode
        ),
        dire_team_evidence=_causal_team_fact(row.dire_team_evidence, cutoff, mode),
    )


def _eligible_history(
    target: DraftTarget, history: Iterable[DraftMapEvidence]
) -> tuple[DraftMapEvidence, ...]:
    cutoff = _utc(target.prediction_cutoff, "prediction_cutoff")
    accepted: dict[str, DraftMapEvidence] = {}
    match_ids: dict[int, DraftMapEvidence] = {}
    for row in history:
        completed = _utc(row.completed_at, "completed_at")
        if row.match_id == target.match_id or completed >= cutoff:
            continue
        if target.availability_mode is AvailabilityMode.PROSPECTIVE:
            if row.first_usable_at is None:
                continue
            if _utc(row.first_usable_at, "first_usable_at") > cutoff:
                continue
        existing = accepted.get(row.evidence_id)
        if existing is not None:
            if _canonical_evidence(existing) != _canonical_evidence(row):
                raise ValueError(f"conflicting evidence_id: {row.evidence_id}")
            continue
        same_match = match_ids.get(row.match_id)
        if same_match is not None:
            if _canonical_evidence(same_match) != _canonical_evidence(row):
                raise ValueError(f"multiple accepted versions for match {row.match_id}")
            continue
        accepted[row.evidence_id] = row
        match_ids[row.match_id] = row
    ordered = sorted(
        accepted.values(),
        key=lambda row: (
            _utc(row.completed_at, "completed_at"),
            row.match_id,
            row.evidence_id,
        ),
    )
    return tuple(
        _causal_history_row(row, cutoff, target.availability_mode)
        for row in ordered
    )


def _side_rows(history: Sequence[DraftMapEvidence]) -> tuple[_SideRow, ...]:
    rows: list[_SideRow] = []
    for match in history:
        rows.extend(
            (
                _SideRow(
                    match.evidence_id,
                    match,
                    match.radiant,
                    match.dire,
                    match.radiant_win,
                    match.radiant_hero_evidence,
                    match.radiant_team_evidence,
                ),
                _SideRow(
                    match.evidence_id,
                    match,
                    match.dire,
                    match.radiant,
                    not match.radiant_win,
                    match.dire_hero_evidence,
                    match.dire_team_evidence,
                ),
            )
        )
    return tuple(rows)


def _posterior(successes: int, opportunities: int) -> float:
    return (successes + 1.0) / (opportunities + 2.0)


def _missing(name: str, reason: str) -> FeatureEstimate:
    return FeatureEstimate(name, None, 0, (), 0.0, reason)


def _estimate(
    name: str,
    value: float | None,
    support: int,
    evidence_ids: Iterable[str],
    coverage: float,
    reason: str | None = None,
) -> FeatureEstimate:
    return FeatureEstimate(
        name=name,
        value=None if value is None else round(float(value), 8),
        support=int(support),
        evidence_ids=tuple(sorted(set(evidence_ids))),
        coverage=round(max(0.0, min(1.0, coverage)), 6),
        missing_reason=reason,
    )


def _count_feature(
    name: str,
    radiant_items: Sequence[Any],
    dire_items: Sequence[Any],
    counter: Callable[[Any], _Count],
    *,
    expected_item_count: int,
    empty_reason: str = "target_input_unavailable",
) -> FeatureEstimate:
    items = (*radiant_items, *dire_items)
    if not items:
        return _missing(name, empty_reason)
    counts = tuple(counter(item) for item in items)
    radiant_counts = counts[: len(radiant_items)]
    dire_counts = counts[len(radiant_items) :]
    if not radiant_counts or not dire_counts:
        return _missing(name, empty_reason)
    radiant = math.fsum(
        _posterior(row.successes, row.opportunities) for row in radiant_counts
    ) / len(radiant_counts)
    dire = math.fsum(
        _posterior(row.successes, row.opportunities) for row in dire_counts
    ) / len(dire_counts)
    support = sum(row.opportunities for row in counts)
    support_coverage = math.fsum(
        min(1.0, row.opportunities / MIN_FEATURE_SUPPORT) for row in counts
    ) / len(counts)
    target_coverage = min(1.0, len(items) / expected_item_count)
    coverage = support_coverage * target_coverage
    reason = "insufficient_historical_support" if coverage < 1.0 else None
    return _estimate(
        name,
        radiant - dire,
        support,
        (evidence for row in counts for evidence in row.evidence_ids),
        coverage,
        reason,
    )


def _hero_outcome_count(
    rows: Sequence[_SideRow], hero_id: int, minimum_duration: int = 0
) -> _Count:
    selected = tuple(
        row
        for row in rows
        if row.match.duration_seconds >= minimum_duration
        and any(player.hero_id == hero_id for player in row.team.players)
    )
    return _Count(
        successes=sum(row.won for row in selected),
        opportunities=len(selected),
        evidence_ids=tuple(row.evidence_id for row in selected),
    )


def _role_outcome_count(
    rows: Sequence[_SideRow], hero_position: tuple[int, int]
) -> _Count:
    hero_id, position = hero_position
    selected = []
    for row in rows:
        fact = row.hero_fact(hero_id)
        if (
            fact is not None
            and fact.observed_position == position
            and fact.observed_position_confidence >= ROLE_CONFIDENCE_MIN
        ):
            selected.append(row)
    return _Count(
        successes=sum(row.won for row in selected),
        opportunities=len(selected),
        evidence_ids=tuple(row.evidence_id for row in selected),
    )


def _synergy_count(rows: Sequence[_SideRow], pair: tuple[int, int]) -> _Count:
    wanted = set(pair)
    selected = tuple(
        row
        for row in rows
        if wanted.issubset(player.hero_id for player in row.team.players)
    )
    return _Count(
        sum(row.won for row in selected),
        len(selected),
        tuple(row.evidence_id for row in selected),
    )


def _counter_feature(
    target: DraftTarget, rows: Sequence[_SideRow]
) -> FeatureEstimate:
    pairs = tuple(
        (radiant.hero_id, dire.hero_id)
        for radiant in target.radiant.players
        for dire in target.dire.players
    )
    counts: list[_Count] = []
    matches = tuple(
        {row.match.match_id: row.match for row in rows}.values()
    )
    for radiant_hero, dire_hero in pairs:
        selected: list[tuple[DraftMapEvidence, bool]] = []
        for match in matches:
            historical_radiant = {
                player.hero_id for player in match.radiant.players
            }
            historical_dire = {player.hero_id for player in match.dire.players}
            if radiant_hero in historical_radiant and dire_hero in historical_dire:
                selected.append((match, match.radiant_win))
            elif radiant_hero in historical_dire and dire_hero in historical_radiant:
                selected.append((match, not match.radiant_win))
        counts.append(
            _Count(
                sum(success for _, success in selected),
                len(selected),
                tuple(match.evidence_id for match, _ in selected),
            )
        )
    mean = math.fsum(
        _posterior(row.successes, row.opportunities) for row in counts
    ) / len(counts)
    support = sum(row.opportunities for row in counts)
    coverage = math.fsum(
        min(1.0, row.opportunities / MIN_FEATURE_SUPPORT) for row in counts
    ) / len(counts)
    return _estimate(
        "counter_win_rate_edge",
        2.0 * (mean - 0.5),
        support,
        (evidence for row in counts for evidence in row.evidence_ids),
        coverage,
        "insufficient_historical_support" if coverage < 1.0 else None,
    )


HeroMetric = Callable[[_SideRow, DraftHeroMapEvidence], float | None]


def _metric_observations(
    rows: Sequence[_SideRow], hero_id: int, metric: HeroMetric
) -> tuple[tuple[float, str], ...]:
    values = []
    for row in rows:
        fact = row.hero_fact(hero_id)
        if fact is None:
            continue
        value = metric(row, fact)
        if value is not None and _finite(value):
            values.append((float(value), row.evidence_id))
    return tuple(values)


def _continuous_hero_feature(
    name: str,
    target: DraftTarget,
    rows: Sequence[_SideRow],
    metric: HeroMetric,
    *,
    relative_to_prior: bool = True,
) -> FeatureEstimate:
    all_values = tuple(
        float(value)
        for row in rows
        for fact in row.heroes
        if (value := metric(row, fact)) is not None and _finite(value)
    )
    if not all_values:
        return _missing(name, "exact_source_unavailable")
    prior = math.fsum(all_values) / len(all_values)

    def side(team: DraftTeam) -> tuple[float, int, tuple[str, ...], float]:
        estimates: list[float] = []
        support = 0
        evidence: list[str] = []
        coverage = 0.0
        for player in team.players:
            observations = _metric_observations(rows, player.hero_id, metric)
            support += len(observations)
            evidence.extend(row[1] for row in observations)
            coverage += min(1.0, len(observations) / MIN_FEATURE_SUPPORT)
            estimates.append(
                (math.fsum(value for value, _ in observations) + prior * SMOOTHING_SUPPORT)
                / (len(observations) + SMOOTHING_SUPPORT)
            )
        return (
            math.fsum(estimates) / len(estimates),
            support,
            tuple(evidence),
            coverage / len(team.players),
        )

    radiant, radiant_n, radiant_ids, radiant_coverage = side(target.radiant)
    dire, dire_n, dire_ids, dire_coverage = side(target.dire)
    scale = max(abs(prior), 1e-12) if relative_to_prior else 1.0
    coverage = (radiant_coverage + dire_coverage) / 2.0
    return _estimate(
        name,
        (radiant - dire) / scale,
        radiant_n + dire_n,
        (*radiant_ids, *dire_ids),
        coverage,
        "insufficient_historical_support" if coverage < 1.0 else None,
    )


def _combine_features(name: str, values: Sequence[FeatureEstimate]) -> FeatureEstimate:
    available = tuple(row for row in values if row.value is not None)
    if not available:
        return _missing(name, "exact_source_unavailable")
    return _estimate(
        name,
        math.fsum(float(row.value) for row in available) / len(available),
        sum(row.support for row in available),
        (evidence for row in available for evidence in row.evidence_ids),
        math.fsum(row.coverage for row in values) / len(values),
        "partial_exact_source_coverage"
        if len(available) < len(values) or any(row.coverage < 1.0 for row in values)
        else None,
    )


def _farm_demand_feature(
    target: DraftTarget, rows: Sequence[_SideRow]
) -> FeatureEstimate:
    shares: dict[int, list[tuple[float, str, int | None, float]]] = {}
    all_shares: list[float] = []
    for row in rows:
        if len(row.heroes) != 5 or any(fact.net_worth is None for fact in row.heroes):
            continue
        total = math.fsum(float(fact.net_worth) for fact in row.heroes)
        if total <= 0:
            continue
        for fact in row.heroes:
            share = float(fact.net_worth) / total
            all_shares.append(share)
            shares.setdefault(fact.hero_id, []).append(
                (
                    share,
                    row.evidence_id,
                    fact.observed_position,
                    fact.observed_position_confidence,
                )
            )
    if not all_shares:
        return _missing("farm_demand_balance_diff", "exact_source_unavailable")
    prior = math.fsum(all_shares) / len(all_shares)

    def side(team: DraftTeam) -> tuple[float, int, tuple[str, ...], float]:
        demand = 0.0
        support = 0
        ids: list[str] = []
        coverage = 0.0
        for player in team.players:
            observations = shares.get(player.hero_id, [])
            if (
                player.expected_position is not None
                and player.expected_position_confidence >= ROLE_CONFIDENCE_MIN
            ):
                observations = [
                    row
                    for row in observations
                    if row[2] == player.expected_position and row[3] >= ROLE_CONFIDENCE_MIN
                ]
            support += len(observations)
            ids.extend(row[1] for row in observations)
            coverage += min(1.0, len(observations) / MIN_FEATURE_SUPPORT)
            demand += (
                math.fsum(row[0] for row in observations) + prior * SMOOTHING_SUPPORT
            ) / (len(observations) + SMOOTHING_SUPPORT)
        return (
            -abs(demand - 1.0),
            support,
            tuple(ids),
            coverage / len(team.players),
        )

    radiant, radiant_n, radiant_ids, radiant_coverage = side(target.radiant)
    dire, dire_n, dire_ids, dire_coverage = side(target.dire)
    coverage = (radiant_coverage + dire_coverage) / 2.0
    return _estimate(
        "farm_demand_balance_diff",
        radiant - dire,
        radiant_n + dire_n,
        (*radiant_ids, *dire_ids),
        coverage,
        "insufficient_historical_support" if coverage < 1.0 else None,
    )


def _long_fight_feature(
    target: DraftTarget, rows: Sequence[_SideRow]
) -> FeatureEstimate:
    def count(hero_id: int) -> _Count:
        successes = opportunities = 0
        evidence: list[str] = []
        for row in rows:
            if not any(player.hero_id == hero_id for player in row.team.players):
                continue
            facts = row.facts
            if (
                facts.long_fight_wins is None
                or facts.long_fight_opportunities is None
                or facts.long_fight_opportunities == 0
            ):
                continue
            successes += facts.long_fight_wins
            opportunities += facts.long_fight_opportunities
            evidence.append(row.evidence_id)
        return _Count(successes, opportunities, tuple(evidence))

    fight = _count_feature(
        "_long_fight",
        tuple(player.hero_id for player in target.radiant.players),
        tuple(player.hero_id for player in target.dire.players),
        count,
        expected_item_count=10,
    )
    buyback = _continuous_hero_feature(
        "_buyback",
        target,
        rows,
        lambda row, fact: (
            None
            if fact.buyback_count is None
            else float(fact.buyback_count) / row.match.duration_seconds
        ),
    )
    return _combine_features("long_fight_buyback_proxy_diff", (fight, buyback))


def _style_rate_feature(
    name: str,
    target: DraftTarget,
    attribute: str,
) -> FeatureEstimate:
    radiant = target.radiant_style
    dire = target.dire_style
    if radiant is None or dire is None:
        return _missing(name, "causal_team_style_snapshot_unavailable")
    radiant_rate = getattr(radiant, attribute)
    dire_rate = getattr(dire, attribute)
    if radiant_rate.value is None or dire_rate.value is None:
        return _missing(name, "team_style_rate_unavailable")
    coverage = min(radiant_rate.coverage, dire_rate.coverage)
    return _estimate(
        name,
        float(radiant_rate.value) - float(dire_rate.value),
        radiant_rate.support + dire_rate.support,
        (radiant.provenance.input_hash, dire.provenance.input_hash),
        coverage,
        "partial_team_style_coverage" if coverage < 1.0 else None,
    )


def _player_form_feature(
    target: DraftTarget, rows: Sequence[_SideRow]
) -> FeatureEstimate:
    all_values = tuple(
        float(fact.execution_score)
        for row in rows
        for fact in row.heroes
        if fact.execution_score is not None
    )
    if not all_values:
        return _missing("context_player_form_diff", "exact_source_unavailable")
    prior = math.fsum(all_values) / len(all_values)

    def side(team: DraftTeam) -> tuple[float, int, tuple[str, ...], float]:
        values = []
        ids: list[str] = []
        support = 0
        coverage = 0.0
        for player in team.players:
            observed = tuple(
                (float(fact.execution_score), row.evidence_id)
                for row in rows
                for fact in row.heroes
                if fact.player_id == player.player_id and fact.execution_score is not None
            )
            support += len(observed)
            ids.extend(evidence for _, evidence in observed)
            coverage += min(1.0, len(observed) / MIN_FEATURE_SUPPORT)
            values.append(
                (math.fsum(value for value, _ in observed) + prior * SMOOTHING_SUPPORT)
                / (len(observed) + SMOOTHING_SUPPORT)
            )
        return math.fsum(values) / 5.0, support, tuple(ids), coverage / 5.0

    radiant, radiant_n, radiant_ids, radiant_coverage = side(target.radiant)
    dire, dire_n, dire_ids, dire_coverage = side(target.dire)
    coverage = (radiant_coverage + dire_coverage) / 2.0
    return _estimate(
        "context_player_form_diff",
        radiant - dire,
        radiant_n + dire_n,
        (*radiant_ids, *dire_ids),
        coverage,
        "insufficient_historical_support" if coverage < 1.0 else None,
    )


def _roster_stability_feature(
    target: DraftTarget, rows: Sequence[_SideRow]
) -> FeatureEstimate:
    def side(team: DraftTeam) -> tuple[float, int, tuple[str, ...], float]:
        current = {player.player_id for player in team.players}
        selected = tuple(row for row in rows if row.team.team_id == team.team_id)
        overlaps = tuple(
            len(current & {player.player_id for player in row.team.players}) / 5.0
            for row in selected
        )
        value = (math.fsum(overlaps) + 0.5 * SMOOTHING_SUPPORT) / (
            len(overlaps) + SMOOTHING_SUPPORT
        )
        return (
            value,
            len(selected),
            tuple(row.evidence_id for row in selected),
            min(1.0, len(selected) / MIN_FEATURE_SUPPORT),
        )

    radiant, radiant_n, radiant_ids, radiant_coverage = side(target.radiant)
    dire, dire_n, dire_ids, dire_coverage = side(target.dire)
    coverage = (radiant_coverage + dire_coverage) / 2.0
    return _estimate(
        "context_roster_stability_diff",
        radiant - dire,
        radiant_n + dire_n,
        (*radiant_ids, *dire_ids),
        coverage,
        "insufficient_historical_support" if coverage < 1.0 else None,
    )


def _patch_feature(
    target: DraftTarget, rows: Sequence[_SideRow]
) -> FeatureEstimate:
    if target.patch is None:
        return _missing("context_patch_adaptation_diff", "target_patch_unavailable")

    def count(team_id: int) -> _Count:
        selected = tuple(
            row
            for row in rows
            if row.team.team_id == team_id and row.match.patch == target.patch
        )
        return _Count(
            sum(row.won for row in selected),
            len(selected),
            tuple(row.evidence_id for row in selected),
        )

    return _count_feature(
        "context_patch_adaptation_diff",
        (target.radiant.team_id,),
        (target.dire.team_id,),
        count,
        expected_item_count=2,
    )


def _opponent_strength_feature(
    target: DraftTarget, rows: Sequence[_SideRow]
) -> FeatureEstimate:
    by_team: dict[int, _Count] = {}
    for team_id in {row.team.team_id for row in rows}:
        selected = tuple(row for row in rows if row.team.team_id == team_id)
        by_team[team_id] = _Count(
            sum(row.won for row in selected),
            len(selected),
            tuple(row.evidence_id for row in selected),
        )

    def side(team_id: int) -> tuple[float, int, tuple[str, ...], float]:
        selected = tuple(row for row in rows if row.team.team_id == team_id)
        if not selected:
            return 0.5, 0, (), 0.0
        strengths = []
        evidence: list[str] = []
        for row in selected:
            count = by_team.get(row.opponent.team_id, _Count(0, 0, ()))
            strengths.append(_posterior(count.successes, count.opportunities))
            evidence.append(row.evidence_id)
            evidence.extend(count.evidence_ids)
        return (
            (math.fsum(strengths) + 0.5 * SMOOTHING_SUPPORT)
            / (len(strengths) + SMOOTHING_SUPPORT),
            len(selected),
            tuple(evidence),
            min(1.0, len(selected) / MIN_FEATURE_SUPPORT),
        )

    radiant, radiant_n, radiant_ids, radiant_coverage = side(target.radiant.team_id)
    dire, dire_n, dire_ids, dire_coverage = side(target.dire.team_id)
    coverage = (radiant_coverage + dire_coverage) / 2.0
    return _estimate(
        "context_opponent_strength_diff",
        radiant - dire,
        radiant_n + dire_n,
        (*radiant_ids, *dire_ids),
        coverage,
        "insufficient_historical_support" if coverage < 1.0 else None,
    )


def _series_feature(
    target: DraftTarget, rows: Sequence[_SideRow]
) -> FeatureEstimate:
    if target.series_id is None:
        return _missing("context_series_prior_win_diff", "target_series_unavailable")

    def count(team_id: int) -> _Count:
        selected = tuple(
            row
            for row in rows
            if row.match.series_id == target.series_id and row.team.team_id == team_id
        )
        return _Count(
            sum(row.won for row in selected),
            len(selected),
            tuple(row.evidence_id for row in selected),
        )

    radiant = count(target.radiant.team_id)
    dire = count(target.dire.team_id)
    support = radiant.opportunities + dire.opportunities
    if support == 0:
        return _missing("context_series_prior_win_diff", "no_earlier_series_map")
    coverage = min(1.0, support / (2.0 * MIN_FEATURE_SUPPORT))
    return _estimate(
        "context_series_prior_win_diff",
        _posterior(radiant.successes, radiant.opportunities)
        - _posterior(dire.successes, dire.opportunities),
        support,
        (*radiant.evidence_ids, *dire.evidence_ids),
        coverage,
        "insufficient_historical_support" if coverage < 1.0 else None,
    )


def _pure_features(
    target: DraftTarget, rows: Sequence[_SideRow]
) -> tuple[FeatureEstimate, ...]:
    radiant_heroes = tuple(player.hero_id for player in target.radiant.players)
    dire_heroes = tuple(player.hero_id for player in target.dire.players)
    radiant_roles = tuple(
        (player.hero_id, player.expected_position)
        for player in target.radiant.players
        if player.expected_position is not None
        and player.expected_position_confidence >= ROLE_CONFIDENCE_MIN
    )
    dire_roles = tuple(
        (player.hero_id, player.expected_position)
        for player in target.dire.players
        if player.expected_position is not None
        and player.expected_position_confidence >= ROLE_CONFIDENCE_MIN
    )
    hero_win = _count_feature(
        "hero_win_rate_diff",
        radiant_heroes,
        dire_heroes,
        lambda hero: _hero_outcome_count(rows, hero),
        expected_item_count=10,
    )
    role_fit = _count_feature(
        "role_fit_win_rate_diff",
        radiant_roles,
        dire_roles,
        lambda hero_role: _role_outcome_count(rows, hero_role),
        expected_item_count=10,
        empty_reason="high_confidence_expected_positions_unavailable",
    )
    synergy = _count_feature(
        "synergy_win_rate_diff",
        tuple(combinations(sorted(radiant_heroes), 2)),
        tuple(combinations(sorted(dire_heroes), 2)),
        lambda pair: _synergy_count(rows, pair),
        expected_item_count=20,
    )
    scaling = _count_feature(
        "scaling_40m_win_rate_diff",
        radiant_heroes,
        dire_heroes,
        lambda hero: _hero_outcome_count(rows, hero, minimum_duration=40 * 60),
        expected_item_count=10,
    )
    control = _continuous_hero_feature(
        "control_initiation_proxy_diff",
        target,
        rows,
        lambda row, fact: (
            None
            if fact.control_seconds is None
            else float(fact.control_seconds) / row.match.duration_seconds
        ),
    )
    sustain = _continuous_hero_feature(
        "save_sustain_proxy_diff",
        target,
        rows,
        lambda row, fact: (
            None
            if fact.hero_healing is None
            else float(fact.hero_healing) / row.match.duration_seconds
        ),
    )
    wave_clear = _continuous_hero_feature(
        "wave_clear_proxy_diff",
        target,
        rows,
        lambda row, fact: (
            None
            if fact.last_hits is None
            else float(fact.last_hits) / row.match.duration_seconds
        ),
    )
    tower = _continuous_hero_feature(
        "_tower_push",
        target,
        rows,
        lambda row, fact: (
            None
            if fact.tower_damage is None
            else float(fact.tower_damage) / row.match.duration_seconds
        ),
    )
    high_ground = _continuous_hero_feature(
        "_high_ground",
        target,
        rows,
        lambda row, _fact: (
            None
            if row.facts.high_ground_events is None
            else float(row.facts.high_ground_events) / row.match.duration_seconds
        ),
    )
    push = _combine_features("push_high_ground_proxy_diff", (tower, high_ground))
    roshan = _continuous_hero_feature(
        "roshan_proxy_diff",
        target,
        rows,
        lambda row, _fact: (
            None
            if row.facts.roshan_events is None
            else float(row.facts.roshan_events) / row.match.duration_seconds
        ),
    )
    return (
        hero_win,
        role_fit,
        synergy,
        _counter_feature(target, rows),
        scaling,
        control,
        sustain,
        wave_clear,
        push,
        roshan,
        _farm_demand_feature(target, rows),
        _long_fight_feature(target, rows),
        _missing(
            "mobility_global_split_diff",
            "exact_mobility_global_split_source_unavailable",
        ),
        _missing(
            "damage_profile_diff",
            "exact_physical_magical_pure_source_unavailable",
        ),
    )


def _context_features(
    target: DraftTarget, rows: Sequence[_SideRow]
) -> tuple[FeatureEstimate, ...]:
    return (
        _style_rate_feature(
            "context_comeback_rate_diff",
            target,
            "comeback_rate",
        ),
        _style_rate_feature(
            "context_throw_resilience_diff",
            target,
            "throw_resilience_rate",
        ),
        _style_rate_feature(
            "context_closeout_rate_diff",
            target,
            "closeout_rate",
        ),
        _player_form_feature(target, rows),
        _roster_stability_feature(target, rows),
        _patch_feature(target, rows),
        _opponent_strength_feature(target, rows),
        _series_feature(target, rows),
    )


def build_draft_feature_snapshot(
    target: DraftTarget, history: Iterable[DraftMapEvidence]
) -> DraftFeatureSnapshot:
    """Build one immutable snapshot from inputs available before its cutoff."""
    eligible = _eligible_history(target, history)
    rows = _side_rows(eligible)
    pure = _pure_features(target, rows)
    context = _context_features(target, rows)
    if tuple(row.name for row in pure) != PURE_FEATURE_SCHEMA:
        raise AssertionError("pure feature implementation does not match schema")
    if tuple(row.name for row in context) != CONTEXT_FEATURE_SCHEMA:
        raise AssertionError("context feature implementation does not match schema")
    payload = {
        "version": FEATURE_VERSION,
        "schema_hash": FEATURE_SCHEMA_HASH,
        "target": _canonical_target(target),
        "eligible_history": [_canonical_evidence(row) for row in eligible],
    }
    input_hash = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    pure_coverage = math.fsum(row.coverage for row in pure) / len(pure)
    context_coverage = math.fsum(row.coverage for row in context) / len(context)
    return DraftFeatureSnapshot(
        match_id=target.match_id,
        prediction_cutoff=_utc(target.prediction_cutoff, "prediction_cutoff"),
        availability_mode=target.availability_mode,
        feature_version=FEATURE_VERSION,
        feature_schema=FEATURE_SCHEMA,
        feature_schema_hash=FEATURE_SCHEMA_HASH,
        input_hash=input_hash,
        pure_features=pure,
        context_features=context,
        evidence_ids=tuple(row.evidence_id for row in eligible),
        support=len(eligible),
        pure_coverage=round(pure_coverage, 6),
        context_coverage=round(context_coverage, 6),
        coverage=round((pure_coverage + context_coverage) / 2.0, 6),
    )
