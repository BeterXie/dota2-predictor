"""Causal, opportunity-conditional Dota 2 team style profiles."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .raw_archive import canonical_json_bytes
from .team_states import TeamMapState


PROFILE_VERSION = "team-style-v2"
HALF_LIFE_DAYS = 45.0
PRIOR_EFFECTIVE_SAMPLE_SIZE = 2.0
MIN_PRIOR_OPPORTUNITIES = 5

EvidenceRef = tuple[int, str, str]


class AvailabilityMode(str, Enum):
    PROSPECTIVE = "prospective"
    RECONSTRUCTED = "reconstructed_walk_forward"


def comeback_metric(threshold: int) -> str:
    return f"comeback_after_{threshold}_deficit"


def throw_metric(threshold: int) -> str:
    return f"throw_after_{threshold}_lead"


CLOSEOUT_5K_RATE = "closeout_after_5000_lead"
REACH_40_RATE = "reach_40_minutes"
REACH_50_RATE = "reach_50_minutes"
ROSHAN_TOWER_RATE = "roshan_to_tower"
ROSHAN_HIGH_GROUND_RATE = "roshan_to_high_ground"
ROSHAN_WIN_RATE = "roshan_to_win"

RATE_METRICS = (
    *(comeback_metric(value) for value in (3_000, 5_000, 10_000)),
    *(throw_metric(value) for value in (3_000, 5_000, 10_000)),
    CLOSEOUT_5K_RATE,
    REACH_40_RATE,
    REACH_50_RATE,
    ROSHAN_TOWER_RATE,
    ROSHAN_HIGH_GROUND_RATE,
    ROSHAN_WIN_RATE,
)

DURATION_GROUPS = (
    "win",
    "loss",
    "advantage",
    "disadvantage",
    "even",
    "closeout_after_5000_lead_win",
)


@dataclass(frozen=True)
class BetaPrior:
    alpha: float
    beta: float
    scope: str = "event_patch"
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        if not _positive_finite(self.alpha) or not _positive_finite(self.beta):
            raise ValueError("Beta prior alpha and beta must be finite and positive")
        if not self.scope:
            raise ValueError("Beta prior scope must be non-empty")


@dataclass(frozen=True)
class ProfileMap:
    state: TeamMapState
    completed_at: datetime
    first_usable_at: datetime | None
    event_id: str
    patch: int | None
    roster: tuple[int, ...] = ()
    opponent_strength_weight: float = 1.0
    event_strength_weight: float = 1.0
    opponent_strength_scope: str = "neutral:unspecified"
    event_strength_scope: str = "neutral:unspecified"
    opponent_strength_evidence: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class MapWeight:
    match_id: int
    state_input_hash: str
    state_version: str
    event_id: str
    patch: int | None
    completed_at: str
    first_usable_at: str | None
    age_weight: float
    roster_overlap_weight: float
    patch_weight: float
    opponent_strength_weight: float
    event_strength_weight: float
    opponent_strength_scope: str
    event_strength_scope: str
    opponent_strength_evidence: tuple[EvidenceRef, ...]
    total_weight: float


@dataclass(frozen=True)
class PosteriorRate:
    metric: str
    opportunities: int
    successes: int
    weighted_opportunities: float
    weighted_successes: float
    prior_alpha: float
    prior_beta: float
    prior_scope: str
    prior_evidence: tuple[EvidenceRef, ...]
    posterior_alpha: float
    posterior_beta: float
    mean: float


@dataclass(frozen=True)
class DurationQuantiles:
    group: str
    count: int
    weighted_count: float
    p25: float | None
    p50: float | None
    p75: float | None


@dataclass(frozen=True)
class TeamStyleProfile:
    team_id: int
    cutoff: datetime
    availability_mode: AvailabilityMode
    profile_version: str
    opportunity_counts: tuple[tuple[str, int], ...]
    posterior_rates: tuple[PosteriorRate, ...]
    duration_quantiles: tuple[DurationQuantiles, ...]
    weighting: tuple[MapWeight, ...]
    effective_sample_size: float
    input_hash: str

    def opportunity_count(self, metric: str) -> int:
        return dict(self.opportunity_counts)[metric]

    def rate(self, metric: str) -> PosteriorRate:
        for value in self.posterior_rates:
            if value.metric == metric:
                return value
        raise KeyError(metric)

    def quantiles(self, group: str) -> DurationQuantiles:
        for value in self.duration_quantiles:
            if value.group == group:
                return value
        raise KeyError(group)


def _positive_finite(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _nonnegative_finite(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field} must be finite and non-negative")
    return float(value)


def _scope(value: Any, field: str) -> str:
    result = str(value).strip()
    if not result:
        raise ValueError(f"{field} must be non-empty")
    return result


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _roster(value: Sequence[int] | None, field: str) -> tuple[int, ...]:
    if not value:
        return ()
    result = tuple(sorted(value))
    if len(result) != len(set(result)) or any(
        isinstance(player, bool) or not isinstance(player, int) or player <= 0
        for player in result
    ):
        raise ValueError(f"{field} must contain unique positive player IDs")
    return result


def _eligible_maps(
    *,
    team_id: int,
    cutoff: datetime,
    mode: AvailabilityMode,
    maps: Iterable[ProfileMap],
) -> tuple[ProfileMap, ...]:
    selected: dict[int, ProfileMap] = {}
    for value in maps:
        completed_at = _utc(value.completed_at, "completed_at")
        first_usable_at = (
            None
            if value.first_usable_at is None
            else _utc(value.first_usable_at, "first_usable_at")
        )
        if value.state.team_id != team_id or value.state.won is None:
            continue
        if completed_at >= cutoff:
            continue
        if first_usable_at is not None and first_usable_at < completed_at:
            raise ValueError(
                f"first_usable_at precedes map completion: {value.state.match_id}"
            )
        if first_usable_at is not None and first_usable_at > cutoff:
            continue
        if mode is AvailabilityMode.PROSPECTIVE and first_usable_at is None:
            continue
        normalized = ProfileMap(
            state=value.state,
            completed_at=completed_at,
            first_usable_at=first_usable_at,
            event_id=str(value.event_id),
            patch=value.patch,
            roster=_roster(value.roster, "map roster"),
            opponent_strength_weight=_nonnegative_finite(
                value.opponent_strength_weight, "opponent strength weight"
            ),
            event_strength_weight=_nonnegative_finite(
                value.event_strength_weight, "event strength weight"
            ),
            opponent_strength_scope=_scope(
                value.opponent_strength_scope, "opponent strength scope"
            ),
            event_strength_scope=_scope(
                value.event_strength_scope, "event strength scope"
            ),
            opponent_strength_evidence=tuple(value.opponent_strength_evidence),
        )
        existing = selected.get(value.state.match_id)
        if existing is not None and existing != normalized:
            raise ValueError(f"conflicting profile map versions: {value.state.match_id}")
        selected[value.state.match_id] = normalized
    return tuple(
        sorted(selected.values(), key=lambda row: (row.completed_at, row.state.match_id))
    )


def _map_weight(
    value: ProfileMap,
    *,
    cutoff: datetime,
    target_roster: tuple[int, ...],
    target_patch: int | None,
) -> MapWeight:
    age_days = (cutoff - value.completed_at).total_seconds() / 86_400
    age_weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
    roster_weight = (
        1.0
        if not target_roster
        else len(set(target_roster).intersection(value.roster)) / len(target_roster)
    )
    patch_weight = (
        1.0
        if target_patch is None
        else 0.0
        if value.patch is None
        else 0.5 ** abs(target_patch - value.patch)
    )
    total = (
        age_weight
        * roster_weight
        * patch_weight
        * value.opponent_strength_weight
        * value.event_strength_weight
    )
    return MapWeight(
        match_id=value.state.match_id,
        state_input_hash=value.state.input_hash,
        state_version=value.state.label_version,
        event_id=value.event_id,
        patch=value.patch,
        completed_at=_datetime_json(value.completed_at) or "",
        first_usable_at=_datetime_json(value.first_usable_at),
        age_weight=age_weight,
        roster_overlap_weight=roster_weight,
        patch_weight=patch_weight,
        opponent_strength_weight=value.opponent_strength_weight,
        event_strength_weight=value.event_strength_weight,
        opponent_strength_scope=value.opponent_strength_scope,
        event_strength_scope=value.event_strength_scope,
        opponent_strength_evidence=value.opponent_strength_evidence,
        total_weight=total,
    )


def _opportunities(state: TeamMapState) -> dict[str, bool]:
    result: dict[str, bool] = {}
    won = bool(state.won)
    for threshold in (3_000, 5_000, 10_000):
        facts = state.threshold(threshold)
        if facts.had_deficit:
            result[comeback_metric(threshold)] = won
        if facts.had_lead:
            result[throw_metric(threshold)] = not won
    five_k = state.threshold(5_000)
    if five_k.had_lead:
        result[CLOSEOUT_5K_RATE] = won
    if state.duration_seconds is not None:
        result[REACH_40_RATE] = state.duration_seconds >= 40 * 60
        result[REACH_50_RATE] = state.duration_seconds >= 50 * 60
    conversion = state.objective_conversion
    if conversion.source_complete and conversion.roshan_opportunity is True:
        result[ROSHAN_TOWER_RATE] = conversion.tower_after_roshan is True
        result[ROSHAN_HIGH_GROUND_RATE] = conversion.high_ground_after_roshan is True
        result[ROSHAN_WIN_RATE] = conversion.win_after_roshan is True
    return result


def derive_causal_event_patch_priors(
    *,
    team_id: int,
    cutoff: datetime,
    maps: Iterable[ProfileMap],
    target_event_id: str,
    target_patch: int | None,
    min_opportunities: int = MIN_PRIOR_OPPORTUNITIES,
) -> dict[str, BetaPrior]:
    """Fit weak empirical priors from earlier, usable non-target-team maps."""
    if min_opportunities < 1:
        raise ValueError("min_opportunities must be positive")
    cutoff = _utc(cutoff, "cutoff")
    eligible = tuple(
        row
        for row in maps
        if row.state.team_id != team_id
        and row.state.opponent_id != team_id
        and row.state.won is not None
        and _utc(row.completed_at, "completed_at") < cutoff
        and row.first_usable_at is not None
        and _utc(row.first_usable_at, "first_usable_at") <= cutoff
    )
    levels = (
        (
            f"event_patch:{target_event_id}:{target_patch}",
            lambda row: row.event_id == target_event_id and row.patch == target_patch,
        ),
        (f"patch:{target_patch}", lambda row: row.patch == target_patch),
        (f"event:{target_event_id}", lambda row: row.event_id == target_event_id),
        ("global", lambda row: True),
    )
    priors: dict[str, BetaPrior] = {}
    for metric in RATE_METRICS:
        selected: tuple[str, int, int, tuple[EvidenceRef, ...]] | None = None
        for scope, predicate in levels:
            outcomes = [
                (values[metric], row)
                for row in eligible
                if predicate(row)
                for values in (_opportunities(row.state),)
                if metric in values
            ]
            if len(outcomes) >= min_opportunities:
                evidence = tuple(
                    sorted(
                        (
                            row.state.match_id,
                            row.state.input_hash,
                            _datetime_json(row.first_usable_at) or "",
                        )
                        for _, row in outcomes
                    )
                )
                selected = (
                    scope,
                    len(outcomes),
                    sum(value for value, _ in outcomes),
                    evidence,
                )
                break
        if selected is None:
            priors[metric] = BetaPrior(
                1.0, 1.0, "neutral:insufficient_causal_event_patch_data"
            )
            continue
        scope, count, successes, evidence = selected
        mean = (successes + 1.0) / (count + 2.0)
        priors[metric] = BetaPrior(
            mean * PRIOR_EFFECTIVE_SAMPLE_SIZE,
            (1.0 - mean) * PRIOR_EFFECTIVE_SAMPLE_SIZE,
            f"{scope}:n={count}",
            evidence,
        )
    return priors


def _weighted_quantile(
    values: Sequence[tuple[float, float]], quantile: float
) -> float | None:
    positive = sorted((value, weight) for value, weight in values if weight > 0)
    total = sum(weight for _, weight in positive)
    if total <= 0:
        return None
    target = quantile * total
    cumulative = 0.0
    for value, weight in positive:
        cumulative += weight
        if cumulative >= target:
            return float(value)
    return float(positive[-1][0])


def _duration_groups(
    maps: Sequence[ProfileMap], weights: Mapping[int, MapWeight]
) -> tuple[DurationQuantiles, ...]:
    values: dict[str, list[tuple[float, float]]] = {name: [] for name in DURATION_GROUPS}
    for item in maps:
        state = item.state
        if state.duration_seconds is None:
            continue
        weight = weights[state.match_id].total_weight
        values["win" if state.won else "loss"].append((state.duration_seconds, weight))
        if state.label.value in {"advantage", "disadvantage", "even"}:
            values[state.label.value].append((state.duration_seconds, weight))
        first_five_k = state.threshold(5_000).first_lead_at
        if state.won and first_five_k is not None:
            values["closeout_after_5000_lead_win"].append(
                (max(0, state.duration_seconds - first_five_k), weight)
            )
    result = []
    for group in DURATION_GROUPS:
        rows = values[group]
        result.append(
            DurationQuantiles(
                group=group,
                count=len(rows),
                weighted_count=sum(weight for _, weight in rows),
                p25=_weighted_quantile(rows, 0.25),
                p50=_weighted_quantile(rows, 0.50),
                p75=_weighted_quantile(rows, 0.75),
            )
        )
    return tuple(result)


def _effective_sample_size(weights: Sequence[MapWeight]) -> float:
    values = tuple(row.total_weight for row in weights if row.total_weight > 0)
    denominator = sum(value * value for value in values)
    return 0.0 if denominator == 0 else sum(values) ** 2 / denominator


def _datetime_json(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _profile_hash(
    *,
    team_id: int,
    cutoff: datetime,
    mode: AvailabilityMode,
    target_roster: tuple[int, ...],
    target_patch: int | None,
    maps: Sequence[ProfileMap],
    weights: Sequence[MapWeight],
    priors: Mapping[str, BetaPrior],
) -> str:
    by_match = {row.match_id: row for row in weights}
    payload = {
        "version": PROFILE_VERSION,
        "team_id": team_id,
        "cutoff": _datetime_json(cutoff),
        "availability_mode": mode.value,
        "half_life_days": HALF_LIFE_DAYS,
        "target_roster": target_roster,
        "target_patch": target_patch,
        "priors": [
            (metric, prior.alpha, prior.beta, prior.scope, prior.evidence)
            for metric, prior in sorted(priors.items())
        ],
        "maps": [
            {
                "match_id": row.state.match_id,
                "state_input_hash": row.state.input_hash,
                "state_version": row.state.label_version,
                "completed_at": _datetime_json(row.completed_at),
                "first_usable_at": _datetime_json(row.first_usable_at),
                "event_id": row.event_id,
                "patch": row.patch,
                "roster": row.roster,
                "weight": vars(by_match[row.state.match_id]),
            }
            for row in maps
        ],
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def build_team_style_profile(
    *,
    team_id: int,
    cutoff: datetime,
    maps: Iterable[ProfileMap],
    priors: Mapping[str, BetaPrior] | None = None,
    target_roster: Sequence[int] | None = None,
    target_patch: int | None = None,
    availability_mode: AvailabilityMode = AvailabilityMode.PROSPECTIVE,
) -> TeamStyleProfile:
    """Build a versioned profile using only facts available at ``cutoff``."""
    if isinstance(team_id, bool) or not isinstance(team_id, int) or team_id <= 0:
        raise ValueError("team_id must be a positive integer")
    cutoff = _utc(cutoff, "cutoff")
    mode = AvailabilityMode(availability_mode)
    roster = _roster(target_roster, "target roster")
    if target_patch is not None and (
        isinstance(target_patch, bool) or not isinstance(target_patch, int) or target_patch <= 0
    ):
        raise ValueError("target_patch must be a positive integer or None")
    eligible = _eligible_maps(team_id=team_id, cutoff=cutoff, mode=mode, maps=maps)
    map_weights = tuple(
        _map_weight(
            value, cutoff=cutoff, target_roster=roster, target_patch=target_patch
        )
        for value in eligible
    )
    weights_by_match = {row.match_id: row for row in map_weights}

    raw_counts = {metric: 0 for metric in RATE_METRICS}
    raw_successes = {metric: 0 for metric in RATE_METRICS}
    weighted_counts = {metric: 0.0 for metric in RATE_METRICS}
    weighted_successes = {metric: 0.0 for metric in RATE_METRICS}
    for value in eligible:
        weight = weights_by_match[value.state.match_id].total_weight
        for metric, succeeded in _opportunities(value.state).items():
            raw_counts[metric] += 1
            raw_successes[metric] += int(succeeded)
            weighted_counts[metric] += weight
            weighted_successes[metric] += weight * int(succeeded)

    default_prior = BetaPrior(1.0, 1.0, "neutral")
    normalized_priors = {
        metric: (priors or {}).get(metric, default_prior) for metric in RATE_METRICS
    }
    posterior = []
    for metric in RATE_METRICS:
        prior = normalized_priors[metric]
        alpha = prior.alpha + weighted_successes[metric]
        beta = prior.beta + weighted_counts[metric] - weighted_successes[metric]
        posterior.append(
            PosteriorRate(
                metric=metric,
                opportunities=raw_counts[metric],
                successes=raw_successes[metric],
                weighted_opportunities=weighted_counts[metric],
                weighted_successes=weighted_successes[metric],
                prior_alpha=prior.alpha,
                prior_beta=prior.beta,
                prior_scope=prior.scope,
                prior_evidence=prior.evidence,
                posterior_alpha=alpha,
                posterior_beta=beta,
                mean=alpha / (alpha + beta),
            )
        )

    input_hash = _profile_hash(
        team_id=team_id,
        cutoff=cutoff,
        mode=mode,
        target_roster=roster,
        target_patch=target_patch,
        maps=eligible,
        weights=map_weights,
        priors=normalized_priors,
    )
    return TeamStyleProfile(
        team_id=team_id,
        cutoff=cutoff,
        availability_mode=mode,
        profile_version=PROFILE_VERSION,
        opportunity_counts=tuple((metric, raw_counts[metric]) for metric in RATE_METRICS),
        posterior_rates=tuple(posterior),
        duration_quantiles=_duration_groups(eligible, weights_by_match),
        weighting=map_weights,
        effective_sample_size=_effective_sample_size(map_weights),
        input_hash=input_hash,
    )
