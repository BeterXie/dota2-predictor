"""Causal C0-C9 lineup features with evidence-aware pair shrinkage."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
from numbers import Real
from typing import Any, Iterable, Mapping, Sequence

from .hero_clusters import (
    BACKOFF_POLICY,
    CLUSTER_IDS,
    LANE_CONFIDENCE_MIN,
    ROLE_CONFIDENCE_MIN,
    ClusterAssignment,
    ClusterEvidenceMode,
    ClusterPairStatistic,
    ClusterResource,
    assign_hero_cluster,
    canonical_hash,
)
UTC = timezone.utc
CLUSTER_FEATURE_VERSION = "cluster-features-v1"
DEFAULT_PAIR_PRIOR_STRENGTH = 20.0
DEFAULT_ROLE_LANE_PRIOR_STRENGTH = 40.0
DEFAULT_GLOBAL_NEUTRAL_PRIOR = 0.5
DEFAULT_MIN_PAIR_SUPPORT = 20

CLUSTER_COUNT_FEATURES = (
    *(f"radiant_cluster_count_{cluster_id}" for cluster_id in CLUSTER_IDS),
    *(f"dire_cluster_count_{cluster_id}" for cluster_id in CLUSTER_IDS),
    *(f"cluster_count_diff_{cluster_id}" for cluster_id in CLUSTER_IDS),
)
CLUSTER_STRUCTURE_FEATURES = (
    "support_cluster_pair_same",
    "support_cluster_pair_interaction",
    "support_cluster_pair_support",
    "support_cluster_pair_coverage",
    "core_pair_mean",
    "core_pair_min",
    "core_pair_max",
    "core_pair_dispersion",
    "core_pair_support",
    "core_pair_coverage",
    "lineup_pair_mean",
    "lineup_pair_min",
    "lineup_pair_dispersion",
    "worst_pair_value",
    "worst_pair_support",
    "repeated_cluster_count",
    "unique_cluster_count",
    "cluster_concentration",
    "cross_team_cluster_edge",
    "cross_team_cluster_min",
    "cross_team_cluster_dispersion",
    "cross_team_cluster_support",
    "mapping_coverage",
    "rank_fallback_ratio",
    "low_support_ratio",
    "assignment_entropy",
    "missing_cluster_count",
)
CLUSTER_FEATURE_SCHEMA = CLUSTER_COUNT_FEATURES + CLUSTER_STRUCTURE_FEATURES
CLUSTER_FEATURE_SCHEMA_HASH = canonical_hash(
    {
        "version": CLUSTER_FEATURE_VERSION,
        "features": list(CLUSTER_FEATURE_SCHEMA),
        "estimate_metadata": [
            "support",
            "effective_support",
            "coverage",
            "missing_reason",
            "evidence_ids",
        ],
    }
)
CLUSTER_MODEL_FEATURE_SCHEMA = tuple(
    projected
    for name in CLUSTER_FEATURE_SCHEMA
    for projected in (
        name,
        f"{name}__log1p_support",
        f"{name}__coverage",
        f"{name}__missing",
    )
)


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _probability(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be between zero and one")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{field} must be between zero and one")
    return result


def _positive_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be positive")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{field} must be positive")
    return result


@dataclass(frozen=True)
class ClusterPlayer:
    hero_id: int
    expected_role: str | None
    expected_lane: str | None
    role_confidence: float
    lane_confidence: float

    def __post_init__(self) -> None:
        _positive_int(self.hero_id, "hero_id")
        _probability(self.role_confidence, "role_confidence")
        _probability(self.lane_confidence, "lane_confidence")

    def to_payload(self) -> dict[str, Any]:
        return {
            "hero_id": self.hero_id,
            "expected_role": self.expected_role,
            "expected_lane": self.expected_lane,
            "role_confidence": self.role_confidence,
            "lane_confidence": self.lane_confidence,
        }


@dataclass(frozen=True)
class ClusterFeatureTarget:
    match_id: int
    prediction_cutoff: datetime
    patch: str
    evidence_mode: ClusterEvidenceMode
    radiant: tuple[ClusterPlayer, ...]
    dire: tuple[ClusterPlayer, ...]

    def __post_init__(self) -> None:
        _positive_int(self.match_id, "match_id")
        object.__setattr__(
            self, "prediction_cutoff", _utc(self.prediction_cutoff, "prediction_cutoff")
        )
        if not isinstance(self.patch, str) or not self.patch.strip():
            raise ValueError("patch must be non-empty")
        if not isinstance(self.evidence_mode, ClusterEvidenceMode):
            raise ValueError("unsupported cluster evidence mode")
        if len(self.radiant) != 5 or len(self.dire) != 5:
            raise ValueError("cluster features require two complete five-hero lineups")
        for side, players in (("radiant", self.radiant), ("dire", self.dire)):
            hero_ids = tuple(player.hero_id for player in players)
            if len(set(hero_ids)) != len(hero_ids):
                raise ValueError(f"{side} hero IDs must be unique")

    def to_payload(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "prediction_cutoff": self.prediction_cutoff.isoformat(),
            "patch": self.patch,
            "evidence_mode": self.evidence_mode.value,
            "radiant": [row.to_payload() for row in self.radiant],
            "dire": [row.to_payload() for row in self.dire],
        }


@dataclass(frozen=True)
class ClusterShrinkageConfig:
    pair_prior_strength: float = DEFAULT_PAIR_PRIOR_STRENGTH
    role_lane_prior_strength: float = DEFAULT_ROLE_LANE_PRIOR_STRENGTH
    global_neutral_prior: float = DEFAULT_GLOBAL_NEUTRAL_PRIOR
    min_pair_support: int = DEFAULT_MIN_PAIR_SUPPORT

    def __post_init__(self) -> None:
        _positive_number(self.pair_prior_strength, "pair_prior_strength")
        _positive_number(self.role_lane_prior_strength, "role_lane_prior_strength")
        _probability(self.global_neutral_prior, "global_neutral_prior")
        _positive_int(self.min_pair_support, "min_pair_support")

    def to_payload(self) -> dict[str, Any]:
        return {
            "pair_prior_strength": self.pair_prior_strength,
            "role_lane_prior_strength": self.role_lane_prior_strength,
            "global_neutral_prior": self.global_neutral_prior,
            "min_pair_support": self.min_pair_support,
        }


@dataclass(frozen=True)
class ClusterFeatureEstimate:
    name: str
    value: float | None
    support: int
    effective_support: float
    coverage: float
    missing_reason: str | None
    evidence_ids: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "support": self.support,
            "effective_support": self.effective_support,
            "coverage": self.coverage,
            "missing_reason": self.missing_reason,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class ClusterFeatureSnapshot:
    match_id: int
    prediction_cutoff: datetime
    patch: str
    evidence_mode: ClusterEvidenceMode
    feature_version: str
    feature_schema: tuple[str, ...]
    feature_schema_hash: str
    input_hash: str
    cluster_resource_version: str
    cluster_resource_hash: str
    radiant_assignments: tuple[ClusterAssignment, ...]
    dire_assignments: tuple[ClusterAssignment, ...]
    features: tuple[ClusterFeatureEstimate, ...]
    mapping_coverage: float
    support: int
    missing_reason: str | None

    def feature(self, name: str) -> ClusterFeatureEstimate:
        for feature in self.features:
            if feature.name == name:
                return feature
        raise KeyError(name)

    def values(self) -> dict[str, float | None]:
        return {feature.name: feature.value for feature in self.features}

    def to_payload(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "prediction_cutoff": self.prediction_cutoff.isoformat(),
            "patch": self.patch,
            "evidence_mode": self.evidence_mode.value,
            "feature_version": self.feature_version,
            "feature_schema": list(self.feature_schema),
            "feature_schema_hash": self.feature_schema_hash,
            "input_hash": self.input_hash,
            "cluster_resource_version": self.cluster_resource_version,
            "cluster_resource_hash": self.cluster_resource_hash,
            "radiant_assignments": [row.to_payload() for row in self.radiant_assignments],
            "dire_assignments": [row.to_payload() for row in self.dire_assignments],
            "features": [row.to_payload() for row in self.features],
            "mapping_coverage": self.mapping_coverage,
            "support": self.support,
            "missing_reason": self.missing_reason,
        }


@dataclass(frozen=True)
class _PairEstimate:
    value: float
    support: int
    effective_support: float
    coverage: float
    missing_reason: str | None
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class _SidePairs:
    estimates: tuple[_PairEstimate, ...]
    expected_pairs: int

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(row.value for row in self.estimates)

    @property
    def coverage(self) -> float:
        if self.expected_pairs == 0:
            return 0.0
        return sum(row.coverage for row in self.estimates) / self.expected_pairs

    @property
    def support(self) -> int:
        return sum(row.support for row in self.estimates)

    @property
    def effective_support(self) -> float:
        return sum(row.effective_support for row in self.estimates)

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted({value for row in self.estimates for value in row.evidence_ids})
        )

    @property
    def missing_reason(self) -> str | None:
        if len(self.estimates) < self.expected_pairs:
            return "partial_cluster_pair_coverage"
        if any(row.missing_reason is not None for row in self.estimates):
            return "cluster_pair_backoff_or_low_support"
        return None


def _estimate(
    name: str,
    value: float | None,
    *,
    support: int,
    effective_support: float,
    coverage: float,
    missing_reason: str | None,
    evidence_ids: Iterable[str],
) -> ClusterFeatureEstimate:
    return ClusterFeatureEstimate(
        name=name,
        value=None if value is None else round(float(value), 8),
        support=int(support),
        effective_support=round(float(effective_support), 6),
        coverage=round(max(0.0, min(1.0, coverage)), 6),
        missing_reason=missing_reason,
        evidence_ids=tuple(sorted(set(evidence_ids))),
    )


def _player_from_payload(value: object, field: str) -> ClusterPlayer:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    expected = {
        "hero_id",
        "expected_role",
        "expected_lane",
        "role_confidence",
        "lane_confidence",
    }
    if set(value) != expected:
        raise ValueError(f"{field} keys do not match")
    return ClusterPlayer(
        hero_id=_positive_int(value["hero_id"], f"{field}.hero_id"),
        expected_role=value["expected_role"],
        expected_lane=value["expected_lane"],
        role_confidence=_probability(
            value["role_confidence"], f"{field}.role_confidence"
        ),
        lane_confidence=_probability(
            value["lane_confidence"], f"{field}.lane_confidence"
        ),
    )


def cluster_feature_target_from_payload(payload: Mapping[str, Any]) -> ClusterFeatureTarget:
    expected = {
        "match_id",
        "prediction_cutoff",
        "patch",
        "evidence_mode",
        "radiant",
        "dire",
    }
    if set(payload) != expected:
        raise ValueError("cluster feature target keys do not match")
    try:
        cutoff = datetime.fromisoformat(
            str(payload["prediction_cutoff"]).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise ValueError("prediction_cutoff must be an ISO timestamp") from error
    radiant = payload["radiant"]
    dire = payload["dire"]
    if not isinstance(radiant, list) or not isinstance(dire, list):
        raise ValueError("cluster lineups must be arrays")
    return ClusterFeatureTarget(
        match_id=_positive_int(payload["match_id"], "match_id"),
        prediction_cutoff=cutoff,
        patch=str(payload["patch"]),
        evidence_mode=ClusterEvidenceMode(payload["evidence_mode"]),
        radiant=tuple(
            _player_from_payload(row, f"radiant[{index}]")
            for index, row in enumerate(radiant)
        ),
        dire=tuple(
            _player_from_payload(row, f"dire[{index}]")
            for index, row in enumerate(dire)
        ),
    )


def cluster_shrinkage_config_from_payload(
    payload: Mapping[str, Any],
) -> ClusterShrinkageConfig:
    expected = {
        "pair_prior_strength",
        "role_lane_prior_strength",
        "global_neutral_prior",
        "min_pair_support",
    }
    if set(payload) != expected:
        raise ValueError("cluster shrinkage configuration keys do not match")
    return ClusterShrinkageConfig(
        pair_prior_strength=payload["pair_prior_strength"],
        role_lane_prior_strength=payload["role_lane_prior_strength"],
        global_neutral_prior=payload["global_neutral_prior"],
        min_pair_support=payload["min_pair_support"],
    )


def _assign_side(
    target: ClusterFeatureTarget,
    resource: ClusterResource,
    players: Sequence[ClusterPlayer],
) -> tuple[ClusterAssignment, ...]:
    return tuple(
        assign_hero_cluster(
            resource,
            hero_id=player.hero_id,
            expected_role=player.expected_role,
            expected_lane=player.expected_lane,
            role_confidence=player.role_confidence,
            lane_confidence=player.lane_confidence,
            prediction_cutoff=target.prediction_cutoff,
            patch=target.patch,
            evidence_mode=target.evidence_mode,
        )
        for player in players
    )


def _weighted_prior(
    rows: Sequence[ClusterPairStatistic], config: ClusterShrinkageConfig
) -> tuple[float, int, tuple[str, ...]]:
    support = sum(row.support for row in rows)
    numerator = sum(row.value * row.support for row in rows)
    value = (
        numerator + config.global_neutral_prior * config.role_lane_prior_strength
    ) / (support + config.role_lane_prior_strength)
    evidence_ids = tuple(sorted({value for row in rows for value in row.evidence_ids}))
    return value, support, evidence_ids


def _pair_key(
    cluster_a: str, cluster_b: str, relationship: str
) -> tuple[str, str]:
    if relationship == "cross_team":
        return cluster_a, cluster_b
    return tuple(sorted((cluster_a, cluster_b)))


def _pair_estimate(
    resource: ClusterResource,
    *,
    relationship: str,
    cluster_a: str,
    cluster_b: str,
    config: ClusterShrinkageConfig,
) -> _PairEstimate:
    wanted_a, wanted_b = _pair_key(cluster_a, cluster_b, relationship)
    relation_rows = tuple(
        row
        for row in resource.pair_statistics
        if row.relationship == relationship and row.level != "global"
    )
    role_prior, role_support, role_ids = _weighted_prior(relation_rows, config)
    pair_rows = tuple(
        row
        for row in relation_rows
        if row.cluster_a is not None
        and row.cluster_b is not None
        and _pair_key(row.cluster_a, row.cluster_b, relationship)
        == (wanted_a, wanted_b)
    )
    direct = next((row for row in pair_rows if row.level == "pair"), None)
    cluster_prior = next(
        (row for row in pair_rows if row.level == "cluster_pair_prior"), None
    )
    if direct is not None:
        prior = role_prior if cluster_prior is None else cluster_prior.value
        value = (
            direct.value * direct.support + prior * config.pair_prior_strength
        ) / (direct.support + config.pair_prior_strength)
        ids = (*direct.evidence_ids, *(cluster_prior.evidence_ids if cluster_prior else role_ids))
        support = direct.support
        effective_support = support + config.pair_prior_strength
        reason = (
            "low_pair_support"
            if direct.support < config.min_pair_support
            else None
        )
    elif cluster_prior is not None:
        value = (
            cluster_prior.value * cluster_prior.support
            + role_prior * config.pair_prior_strength
        ) / (cluster_prior.support + config.pair_prior_strength)
        ids = (*cluster_prior.evidence_ids, *role_ids)
        support = cluster_prior.support
        effective_support = support + config.pair_prior_strength
        reason = (
            "low_cluster_pair_support"
            if cluster_prior.support < config.min_pair_support
            else None
        )
    elif relation_rows:
        value = role_prior
        ids = role_ids
        support = 0
        effective_support = role_support + config.role_lane_prior_strength
        reason = "cluster_pair_unavailable_role_lane_backoff"
    else:
        value = config.global_neutral_prior
        ids = ()
        support = 0
        effective_support = config.role_lane_prior_strength
        reason = "cluster_pair_unavailable_global_backoff"
    return _PairEstimate(
        value=round(value, 8),
        support=support,
        effective_support=effective_support,
        coverage=min(1.0, support / config.min_pair_support),
        missing_reason=reason,
        evidence_ids=tuple(sorted(set(ids))),
    )


def _side_pairs(
    resource: ClusterResource,
    assignments: Sequence[ClusterAssignment],
    *,
    relationship: str,
    expected_pairs: int,
    config: ClusterShrinkageConfig,
) -> _SidePairs:
    available = tuple(row for row in assignments if row.available)
    estimates = []
    for first, second in combinations(available, 2):
        pair_relationship = relationship
        if relationship == "lineup_pair" and first.expected_role == second.expected_role:
            if first.expected_role == "core":
                pair_relationship = "core_pair"
            elif first.expected_role == "support":
                pair_relationship = "support_pair"
        estimates.append(
            _pair_estimate(
                resource,
                relationship=pair_relationship,
                cluster_a=first.cluster_id,
                cluster_b=second.cluster_id,
                config=config,
            )
        )
    return _SidePairs(estimates=tuple(estimates), expected_pairs=expected_pairs)


def _dispersion(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _pair_diff_feature(
    name: str,
    radiant: _SidePairs,
    dire: _SidePairs,
    reducer: Any,
) -> ClusterFeatureEstimate:
    values_available = bool(radiant.values and dire.values)
    value = None
    if values_available:
        value = reducer(radiant.values) - reducer(dire.values)
    return _estimate(
        name,
        value,
        support=radiant.support + dire.support,
        effective_support=radiant.effective_support + dire.effective_support,
        coverage=(radiant.coverage + dire.coverage) / 2.0,
        missing_reason=(
            radiant.missing_reason or dire.missing_reason
            if values_available
            else "cluster_pair_assignments_unavailable"
        ),
        evidence_ids=(*radiant.evidence_ids, *dire.evidence_ids),
    )


def _count_features(
    radiant: Sequence[ClusterAssignment], dire: Sequence[ClusterAssignment]
) -> tuple[ClusterFeatureEstimate, ...]:
    radiant_counts = Counter(row.cluster_id for row in radiant if row.available)
    dire_counts = Counter(row.cluster_id for row in dire if row.available)
    coverage = (sum(row.coverage for row in radiant) + sum(row.coverage for row in dire)) / 10
    support = sum(row.mapping_support for row in (*radiant, *dire))
    ids = tuple(row.evidence_ids for row in (*radiant, *dire))
    evidence_ids = tuple(value for values in ids for value in values)
    reason = None if all(row.available for row in (*radiant, *dire)) else "partial_cluster_assignment"
    feature_values: dict[str, int] = {}
    for cluster_id in CLUSTER_IDS:
        radiant_value = radiant_counts[cluster_id]
        dire_value = dire_counts[cluster_id]
        feature_values[f"radiant_cluster_count_{cluster_id}"] = radiant_value
        feature_values[f"dire_cluster_count_{cluster_id}"] = dire_value
        feature_values[f"cluster_count_diff_{cluster_id}"] = (
            radiant_value - dire_value
        )
    return tuple(
        _estimate(
            name,
            feature_values[name],
            support=support,
            effective_support=float(support),
            coverage=coverage,
            missing_reason=reason,
            evidence_ids=evidence_ids,
        )
        for name in CLUSTER_COUNT_FEATURES
    )


def _support_features(
    resource: ClusterResource,
    radiant: Sequence[ClusterAssignment],
    dire: Sequence[ClusterAssignment],
    config: ClusterShrinkageConfig,
) -> tuple[ClusterFeatureEstimate, ...]:
    radiant_supports = tuple(
        row for row in radiant if row.available and row.expected_role == "support"
    )
    dire_supports = tuple(
        row for row in dire if row.available and row.expected_role == "support"
    )
    radiant_pairs = _side_pairs(
        resource,
        radiant_supports,
        relationship="support_pair",
        expected_pairs=1,
        config=config,
    )
    dire_pairs = _side_pairs(
        resource,
        dire_supports,
        relationship="support_pair",
        expected_pairs=1,
        config=config,
    )
    complete = len(radiant_supports) == len(dire_supports) == 2
    same = (
        None
        if not complete
        else float(radiant_supports[0].cluster_id == radiant_supports[1].cluster_id)
        - float(dire_supports[0].cluster_id == dire_supports[1].cluster_id)
    )
    support = radiant_pairs.support + dire_pairs.support
    effective = radiant_pairs.effective_support + dire_pairs.effective_support
    coverage = (radiant_pairs.coverage + dire_pairs.coverage) / 2.0
    reason = radiant_pairs.missing_reason or dire_pairs.missing_reason
    ids = (*radiant_pairs.evidence_ids, *dire_pairs.evidence_ids)
    return (
        _estimate(
            "support_cluster_pair_same",
            same,
            support=support,
            effective_support=effective,
            coverage=coverage,
            missing_reason=(reason if complete else "support_assignments_unavailable"),
            evidence_ids=ids,
        ),
        _pair_diff_feature(
            "support_cluster_pair_interaction",
            radiant_pairs,
            dire_pairs,
            lambda values: sum(values) / len(values),
        ),
        _estimate(
            "support_cluster_pair_support",
            support / 2.0 if complete else None,
            support=support,
            effective_support=effective,
            coverage=coverage,
            missing_reason=(reason if complete else "support_assignments_unavailable"),
            evidence_ids=ids,
        ),
        _estimate(
            "support_cluster_pair_coverage",
            coverage,
            support=support,
            effective_support=effective,
            coverage=coverage,
            missing_reason=reason,
            evidence_ids=ids,
        ),
    )


def _core_features(
    resource: ClusterResource,
    radiant: Sequence[ClusterAssignment],
    dire: Sequence[ClusterAssignment],
    config: ClusterShrinkageConfig,
) -> tuple[ClusterFeatureEstimate, ...]:
    radiant_cores = tuple(
        row for row in radiant if row.available and row.expected_role == "core"
    )
    dire_cores = tuple(
        row for row in dire if row.available and row.expected_role == "core"
    )
    radiant_pairs = _side_pairs(
        resource,
        radiant_cores,
        relationship="core_pair",
        expected_pairs=3,
        config=config,
    )
    dire_pairs = _side_pairs(
        resource,
        dire_cores,
        relationship="core_pair",
        expected_pairs=3,
        config=config,
    )
    support = radiant_pairs.support + dire_pairs.support
    effective = radiant_pairs.effective_support + dire_pairs.effective_support
    coverage = (radiant_pairs.coverage + dire_pairs.coverage) / 2.0
    reason = radiant_pairs.missing_reason or dire_pairs.missing_reason
    ids = (*radiant_pairs.evidence_ids, *dire_pairs.evidence_ids)
    return (
        _pair_diff_feature("core_pair_mean", radiant_pairs, dire_pairs, lambda x: sum(x) / len(x)),
        _pair_diff_feature("core_pair_min", radiant_pairs, dire_pairs, min),
        _pair_diff_feature("core_pair_max", radiant_pairs, dire_pairs, max),
        _pair_diff_feature("core_pair_dispersion", radiant_pairs, dire_pairs, _dispersion),
        _estimate(
            "core_pair_support",
            support / 6.0 if radiant_pairs.values and dire_pairs.values else None,
            support=support,
            effective_support=effective,
            coverage=coverage,
            missing_reason=reason,
            evidence_ids=ids,
        ),
        _estimate(
            "core_pair_coverage",
            coverage,
            support=support,
            effective_support=effective,
            coverage=coverage,
            missing_reason=reason,
            evidence_ids=ids,
        ),
    )


def _side_cluster_shape(
    assignments: Sequence[ClusterAssignment],
) -> tuple[int, int, float]:
    counts = Counter(row.cluster_id for row in assignments if row.available)
    total = sum(counts.values())
    if total == 0:
        return 0, 0, 0.0
    repeated = sum(max(0, count - 1) for count in counts.values())
    concentration = sum((count / total) ** 2 for count in counts.values())
    return repeated, len(counts), concentration


def _lineup_features(
    resource: ClusterResource,
    radiant: Sequence[ClusterAssignment],
    dire: Sequence[ClusterAssignment],
    config: ClusterShrinkageConfig,
) -> tuple[ClusterFeatureEstimate, ...]:
    radiant_pairs = _side_pairs(
        resource,
        radiant,
        relationship="lineup_pair",
        expected_pairs=10,
        config=config,
    )
    dire_pairs = _side_pairs(
        resource,
        dire,
        relationship="lineup_pair",
        expected_pairs=10,
        config=config,
    )
    support = radiant_pairs.support + dire_pairs.support
    effective = radiant_pairs.effective_support + dire_pairs.effective_support
    coverage = (radiant_pairs.coverage + dire_pairs.coverage) / 2.0
    reason = radiant_pairs.missing_reason or dire_pairs.missing_reason
    ids = (*radiant_pairs.evidence_ids, *dire_pairs.evidence_ids)
    radiant_shape = _side_cluster_shape(radiant)
    dire_shape = _side_cluster_shape(dire)
    assignment_coverage = (
        sum(row.coverage for row in radiant) + sum(row.coverage for row in dire)
    ) / 10.0
    assignment_reason = (
        None
        if all(row.available for row in (*radiant, *dire))
        else "partial_cluster_assignment"
    )
    assignment_support = sum(row.mapping_support for row in (*radiant, *dire))
    assignment_ids = tuple(
        value for row in (*radiant, *dire) for value in row.evidence_ids
    )
    return (
        _pair_diff_feature("lineup_pair_mean", radiant_pairs, dire_pairs, lambda x: sum(x) / len(x)),
        _pair_diff_feature("lineup_pair_min", radiant_pairs, dire_pairs, min),
        _pair_diff_feature("lineup_pair_dispersion", radiant_pairs, dire_pairs, _dispersion),
        _pair_diff_feature("worst_pair_value", radiant_pairs, dire_pairs, min),
        _estimate(
            "worst_pair_support",
            min((row.support for row in (*radiant_pairs.estimates, *dire_pairs.estimates)), default=0),
            support=support,
            effective_support=effective,
            coverage=coverage,
            missing_reason=reason,
            evidence_ids=ids,
        ),
        _estimate(
            "repeated_cluster_count",
            radiant_shape[0] - dire_shape[0],
            support=assignment_support,
            effective_support=float(assignment_support),
            coverage=assignment_coverage,
            missing_reason=assignment_reason,
            evidence_ids=assignment_ids,
        ),
        _estimate(
            "unique_cluster_count",
            radiant_shape[1] - dire_shape[1],
            support=assignment_support,
            effective_support=float(assignment_support),
            coverage=assignment_coverage,
            missing_reason=assignment_reason,
            evidence_ids=assignment_ids,
        ),
        _estimate(
            "cluster_concentration",
            radiant_shape[2] - dire_shape[2],
            support=assignment_support,
            effective_support=float(assignment_support),
            coverage=assignment_coverage,
            missing_reason=assignment_reason,
            evidence_ids=assignment_ids,
        ),
    )


def _cross_features(
    resource: ClusterResource,
    radiant: Sequence[ClusterAssignment],
    dire: Sequence[ClusterAssignment],
    config: ClusterShrinkageConfig,
) -> tuple[ClusterFeatureEstimate, ...]:
    radiant_available = tuple(row for row in radiant if row.available)
    dire_available = tuple(row for row in dire if row.available)
    pairs = tuple(
        _pair_estimate(
            resource,
            relationship="cross_team",
            cluster_a=radiant_row.cluster_id,
            cluster_b=dire_row.cluster_id,
            config=config,
        )
        for radiant_row in radiant_available
        for dire_row in dire_available
    )
    values = tuple(row.value - 0.5 for row in pairs)
    support = sum(row.support for row in pairs)
    effective = sum(row.effective_support for row in pairs)
    coverage = sum(row.coverage for row in pairs) / 25.0
    reason = (
        "cross_team_cluster_statistics_unavailable"
        if len(pairs) < 25 or any(row.missing_reason for row in pairs)
        else None
    )
    ids = tuple(value for row in pairs for value in row.evidence_ids)
    return (
        _estimate(
            "cross_team_cluster_edge",
            sum(values) / len(values) if values else None,
            support=support,
            effective_support=effective,
            coverage=coverage,
            missing_reason=reason,
            evidence_ids=ids,
        ),
        _estimate(
            "cross_team_cluster_min",
            min(values) if values else None,
            support=support,
            effective_support=effective,
            coverage=coverage,
            missing_reason=reason,
            evidence_ids=ids,
        ),
        _estimate(
            "cross_team_cluster_dispersion",
            _dispersion(values) if values else None,
            support=support,
            effective_support=effective,
            coverage=coverage,
            missing_reason=reason,
            evidence_ids=ids,
        ),
        _estimate(
            "cross_team_cluster_support",
            support / len(pairs) if pairs else None,
            support=support,
            effective_support=effective,
            coverage=coverage,
            missing_reason=reason,
            evidence_ids=ids,
        ),
    )


def _quality_features(
    assignments: Sequence[ClusterAssignment],
    config: ClusterShrinkageConfig,
) -> tuple[ClusterFeatureEstimate, ...]:
    available = tuple(row for row in assignments if row.available)
    mapping_coverage = sum(row.coverage for row in assignments) / len(assignments)
    rank_ratio = sum(row.assignment_source == "rank_backoff" for row in assignments) / len(assignments)
    low_support_ratio = (
        sum(row.mapping_support < config.min_pair_support for row in available)
        / len(available)
        if available
        else 1.0
    )
    counts = Counter(row.cluster_id for row in available)
    entropy = 0.0
    if available and len(counts) > 1:
        raw = -sum(
            (count / len(available)) * math.log(count / len(available))
            for count in counts.values()
        )
        entropy = raw / math.log(len(CLUSTER_IDS))
    support = sum(row.mapping_support for row in available)
    ids = tuple(value for row in available for value in row.evidence_ids)
    reason = None if len(available) == len(assignments) else "partial_cluster_assignment"
    values = (
        ("mapping_coverage", mapping_coverage),
        ("rank_fallback_ratio", rank_ratio),
        ("low_support_ratio", low_support_ratio),
        ("assignment_entropy", entropy),
        ("missing_cluster_count", float(len(assignments) - len(available))),
    )
    return tuple(
        _estimate(
            name,
            value,
            support=support,
            effective_support=float(support),
            coverage=mapping_coverage,
            missing_reason=reason,
            evidence_ids=ids,
        )
        for name, value in values
    )


def build_cluster_feature_snapshot(
    target: ClusterFeatureTarget,
    resource: ClusterResource,
    *,
    config: ClusterShrinkageConfig | None = None,
) -> ClusterFeatureSnapshot:
    """Build a snapshot using only the resource legal at the target cutoff."""

    config = config or ClusterShrinkageConfig()
    radiant = _assign_side(target, resource, target.radiant)
    dire = _assign_side(target, resource, target.dire)
    features = (
        *_count_features(radiant, dire),
        *_support_features(resource, radiant, dire, config),
        *_core_features(resource, radiant, dire, config),
        *_lineup_features(resource, radiant, dire, config),
        *_cross_features(resource, radiant, dire, config),
        *_quality_features((*radiant, *dire), config),
    )
    if tuple(row.name for row in features) != CLUSTER_FEATURE_SCHEMA:
        raise AssertionError("cluster feature implementation does not match schema")
    mapping_coverage = sum(row.coverage for row in (*radiant, *dire)) / 10.0
    input_hash = canonical_hash(
        {
            "target": target.to_payload(),
            "cluster_resource_hash": resource.resource_hash,
            "configuration": config.to_payload(),
            "backoff_policy": list(BACKOFF_POLICY),
            "role_confidence_min": ROLE_CONFIDENCE_MIN,
            "lane_confidence_min": LANE_CONFIDENCE_MIN,
        }
    )
    return ClusterFeatureSnapshot(
        match_id=target.match_id,
        prediction_cutoff=target.prediction_cutoff,
        patch=target.patch,
        evidence_mode=target.evidence_mode,
        feature_version=CLUSTER_FEATURE_VERSION,
        feature_schema=CLUSTER_FEATURE_SCHEMA,
        feature_schema_hash=CLUSTER_FEATURE_SCHEMA_HASH,
        input_hash=input_hash,
        cluster_resource_version=resource.resource_version,
        cluster_resource_hash=resource.resource_hash,
        radiant_assignments=radiant,
        dire_assignments=dire,
        features=features,
        mapping_coverage=round(mapping_coverage, 6),
        support=sum(row.mapping_support for row in (*radiant, *dire)),
        missing_reason=(
            None
            if all(row.available for row in (*radiant, *dire))
            else next(
                row.missing_reason
                for row in (*radiant, *dire)
                if row.missing_reason is not None
            )
        ),
    )


def project_cluster_features(
    snapshot: ClusterFeatureSnapshot,
) -> dict[str, float | None]:
    """Project each estimate as value, support, coverage, and missing flag."""

    if (
        snapshot.feature_version != CLUSTER_FEATURE_VERSION
        or snapshot.feature_schema != CLUSTER_FEATURE_SCHEMA
        or snapshot.feature_schema_hash != CLUSTER_FEATURE_SCHEMA_HASH
    ):
        raise ValueError("cluster feature snapshot schema does not match")
    projected: dict[str, float | None] = {}
    for feature in snapshot.features:
        projected[feature.name] = feature.value
        projected[f"{feature.name}__log1p_support"] = math.log1p(feature.support)
        projected[f"{feature.name}__coverage"] = feature.coverage
        projected[f"{feature.name}__missing"] = float(
            feature.value is None or feature.missing_reason is not None
        )
    if tuple(projected) != CLUSTER_MODEL_FEATURE_SCHEMA:
        raise AssertionError("cluster model projection does not match schema")
    return projected


def cluster_snapshot_hash(snapshot: ClusterFeatureSnapshot) -> str:
    return canonical_hash(snapshot.to_payload())


__all__ = [
    "CLUSTER_COUNT_FEATURES",
    "CLUSTER_FEATURE_SCHEMA",
    "CLUSTER_FEATURE_SCHEMA_HASH",
    "CLUSTER_FEATURE_VERSION",
    "CLUSTER_MODEL_FEATURE_SCHEMA",
    "ClusterFeatureEstimate",
    "ClusterFeatureSnapshot",
    "ClusterFeatureTarget",
    "ClusterPlayer",
    "ClusterShrinkageConfig",
    "build_cluster_feature_snapshot",
    "cluster_feature_target_from_payload",
    "cluster_shrinkage_config_from_payload",
    "cluster_snapshot_hash",
    "project_cluster_features",
]
