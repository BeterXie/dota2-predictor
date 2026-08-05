"""Versioned, causal hero-role-lane cluster assignments."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from numbers import Real
from pathlib import Path
from typing import Any, Mapping

from .raw_archive import canonical_json_bytes


UTC = timezone.utc
CLUSTER_IDS = tuple(f"C{index}" for index in range(10))
UNAVAILABLE_CLUSTER = "unavailable"
CLUSTER_MAPPING_SCHEMA_VERSION = "hero-role-lane-cluster/v1"
DEFAULT_CLUSTER_RESOURCE_PATH = (
    Path(__file__).parent / "resources" / "noxville_clusters_7_41_v1.json"
)
ROLE_CONFIDENCE_MIN = 0.7
LANE_CONFIDENCE_MIN = 0.7
BACKOFF_POLICY = ("hero_role_lane", "hero_role", "hero", "unavailable")

_RESOURCE_FIELDS = frozenset(
    {
        "resource_version",
        "patch_scope",
        "source_identity",
        "published_at",
        "mapping_schema_version",
        "evidence_mode",
        "training_cutoff",
        "status",
        "missing_reason",
        "source_hash",
        "mappings",
        "pair_statistics",
    }
)
_MAPPING_FIELDS = frozenset(
    {
        "hero_id",
        "role",
        "lane",
        "cluster_id",
        "support",
        "mapping_confidence",
        "source_hash",
        "evidence_ids",
    }
)
_PAIR_FIELDS = frozenset(
    {
        "relationship",
        "level",
        "cluster_a",
        "cluster_b",
        "role",
        "lane",
        "value",
        "support",
        "source_hash",
        "evidence_ids",
    }
)
_ROLES = frozenset({"core", "support"})
_LANES = frozenset({"safe", "off", "mid", "roam"})
_RELATIONSHIPS = frozenset({"support_pair", "core_pair", "lineup_pair", "cross_team"})
_PAIR_LEVELS = frozenset({"pair", "cluster_pair_prior", "role_lane_prior", "global"})


class ClusterEvidenceMode(str, Enum):
    PUBLISHED_STATIC = "published_static"
    RECONSTRUCTED_WALK_FORWARD = "reconstructed_walk_forward"


class ClusterResourceStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


def canonical_hash(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    return _utc(parsed, field)


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value


def _optional_nonempty(value: object, field: str) -> str | None:
    return None if value is None else _nonempty(value, field)


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _probability(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be between zero and one")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{field} must be between zero and one")
    return result


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _exact_object(
    value: object, fields: frozenset[str], label: str
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


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    result = tuple(_nonempty(item, field) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} must not contain duplicates")
    return result


@dataclass(frozen=True)
class ClusterMapping:
    hero_id: int
    role: str
    lane: str
    cluster_id: str
    support: int
    mapping_confidence: float
    source_hash: str
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _integer(self.hero_id, "mapping.hero_id", minimum=1)
        if self.role not in _ROLES:
            raise ValueError("mapping.role is unsupported")
        if self.lane not in _LANES:
            raise ValueError("mapping.lane is unsupported")
        if self.cluster_id not in CLUSTER_IDS:
            raise ValueError("mapping.cluster_id is unsupported")
        _integer(self.support, "mapping.support")
        _probability(self.mapping_confidence, "mapping.mapping_confidence")
        _digest(self.source_hash, "mapping.source_hash")
        if any(not isinstance(value, str) or not value for value in self.evidence_ids):
            raise ValueError("mapping.evidence_ids must contain non-empty strings")

    def to_payload(self) -> dict[str, Any]:
        return {
            "hero_id": self.hero_id,
            "role": self.role,
            "lane": self.lane,
            "cluster_id": self.cluster_id,
            "support": self.support,
            "mapping_confidence": self.mapping_confidence,
            "source_hash": self.source_hash,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class ClusterPairStatistic:
    relationship: str
    level: str
    cluster_a: str | None
    cluster_b: str | None
    role: str | None
    lane: str | None
    value: float
    support: int
    source_hash: str
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.relationship not in _RELATIONSHIPS:
            raise ValueError("pair statistic relationship is unsupported")
        if self.level not in _PAIR_LEVELS:
            raise ValueError("pair statistic level is unsupported")
        for field, value in (("cluster_a", self.cluster_a), ("cluster_b", self.cluster_b)):
            if value is not None and value not in CLUSTER_IDS:
                raise ValueError(f"pair statistic {field} is unsupported")
        if self.level in {"pair", "cluster_pair_prior"} and (
            self.cluster_a is None or self.cluster_b is None
        ):
            raise ValueError("pair-level statistics require two clusters")
        if self.role is not None and self.role not in _ROLES:
            raise ValueError("pair statistic role is unsupported")
        if self.lane is not None and self.lane not in _LANES:
            raise ValueError("pair statistic lane is unsupported")
        _probability(self.value, "pair statistic value")
        _integer(self.support, "pair statistic support")
        _digest(self.source_hash, "pair statistic source_hash")
        if any(not isinstance(value, str) or not value for value in self.evidence_ids):
            raise ValueError("pair statistic evidence_ids must contain non-empty strings")

    def to_payload(self) -> dict[str, Any]:
        return {
            "relationship": self.relationship,
            "level": self.level,
            "cluster_a": self.cluster_a,
            "cluster_b": self.cluster_b,
            "role": self.role,
            "lane": self.lane,
            "value": self.value,
            "support": self.support,
            "source_hash": self.source_hash,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class ClusterResource:
    resource_version: str
    patch_scope: tuple[str, ...]
    source_identity: str
    published_at: datetime | None
    mapping_schema_version: str
    evidence_mode: ClusterEvidenceMode
    training_cutoff: datetime | None
    status: ClusterResourceStatus
    missing_reason: str | None
    source_hash: str | None
    mappings: tuple[ClusterMapping, ...]
    pair_statistics: tuple[ClusterPairStatistic, ...]

    def __post_init__(self) -> None:
        _nonempty(self.resource_version, "resource_version")
        if not self.patch_scope or len(set(self.patch_scope)) != len(self.patch_scope):
            raise ValueError("patch_scope must contain unique patches")
        for patch in self.patch_scope:
            _nonempty(patch, "patch_scope")
        _nonempty(self.source_identity, "source_identity")
        if self.published_at is not None:
            object.__setattr__(self, "published_at", _utc(self.published_at, "published_at"))
        if self.mapping_schema_version != CLUSTER_MAPPING_SCHEMA_VERSION:
            raise ValueError("unsupported cluster mapping schema version")
        if not isinstance(self.evidence_mode, ClusterEvidenceMode):
            raise ValueError("unsupported cluster evidence mode")
        if self.training_cutoff is not None:
            object.__setattr__(
                self, "training_cutoff", _utc(self.training_cutoff, "training_cutoff")
            )
        if not isinstance(self.status, ClusterResourceStatus):
            raise ValueError("unsupported cluster resource status")
        if self.source_hash is not None:
            _digest(self.source_hash, "source_hash")
        keys = tuple((row.hero_id, row.role, row.lane) for row in self.mappings)
        if len(keys) != len(set(keys)):
            raise ValueError("cluster mappings contain duplicate hero-role-lane keys")
        pair_keys = tuple(
            (
                row.relationship,
                row.level,
                row.cluster_a,
                row.cluster_b,
                row.role,
                row.lane,
            )
            for row in self.pair_statistics
        )
        if len(pair_keys) != len(set(pair_keys)):
            raise ValueError("cluster pair statistics contain duplicate keys")
        if self.status is ClusterResourceStatus.AVAILABLE:
            if self.missing_reason is not None or self.source_hash is None or not self.mappings:
                raise ValueError("available cluster resources need mappings and source authority")
            if (
                self.evidence_mode is ClusterEvidenceMode.PUBLISHED_STATIC
                and self.published_at is None
            ):
                raise ValueError("published resources require published_at")
            if (
                self.evidence_mode is ClusterEvidenceMode.RECONSTRUCTED_WALK_FORWARD
                and self.training_cutoff is None
            ):
                raise ValueError("reconstructed resources require training_cutoff")
        elif self.missing_reason is None:
            raise ValueError("unavailable cluster resources require missing_reason")

    def to_payload(self) -> dict[str, Any]:
        return {
            "resource_version": self.resource_version,
            "patch_scope": list(self.patch_scope),
            "source_identity": self.source_identity,
            "published_at": None if self.published_at is None else self.published_at.isoformat(),
            "mapping_schema_version": self.mapping_schema_version,
            "evidence_mode": self.evidence_mode.value,
            "training_cutoff": (
                None if self.training_cutoff is None else self.training_cutoff.isoformat()
            ),
            "status": self.status.value,
            "missing_reason": self.missing_reason,
            "source_hash": self.source_hash,
            "mappings": [row.to_payload() for row in self.mappings],
            "pair_statistics": [row.to_payload() for row in self.pair_statistics],
        }

    @property
    def resource_hash(self) -> str:
        return canonical_hash(self.to_payload())


@dataclass(frozen=True)
class ClusterAssignment:
    hero_id: int
    expected_role: str | None
    expected_lane: str | None
    cluster_id: str
    mapping_support: int
    mapping_confidence: float
    assignment_source: str
    missing_reason: str | None
    evidence_ids: tuple[str, ...]
    coverage: float

    @property
    def available(self) -> bool:
        return self.cluster_id != UNAVAILABLE_CLUSTER

    def to_payload(self) -> dict[str, Any]:
        return {
            "hero_id": self.hero_id,
            "expected_role": self.expected_role,
            "expected_lane": self.expected_lane,
            "cluster_id": self.cluster_id,
            "mapping_support": self.mapping_support,
            "mapping_confidence": self.mapping_confidence,
            "assignment_source": self.assignment_source,
            "missing_reason": self.missing_reason,
            "evidence_ids": list(self.evidence_ids),
            "coverage": self.coverage,
        }


def _mapping_from_payload(value: object) -> ClusterMapping:
    row = _exact_object(value, _MAPPING_FIELDS, "cluster mapping")
    return ClusterMapping(
        hero_id=_integer(row["hero_id"], "mapping.hero_id", minimum=1),
        role=_nonempty(row["role"], "mapping.role"),
        lane=_nonempty(row["lane"], "mapping.lane"),
        cluster_id=_nonempty(row["cluster_id"], "mapping.cluster_id"),
        support=_integer(row["support"], "mapping.support"),
        mapping_confidence=_probability(
            row["mapping_confidence"], "mapping.mapping_confidence"
        ),
        source_hash=_digest(row["source_hash"], "mapping.source_hash"),
        evidence_ids=_string_tuple(row["evidence_ids"], "mapping.evidence_ids"),
    )


def _pair_from_payload(value: object) -> ClusterPairStatistic:
    row = _exact_object(value, _PAIR_FIELDS, "cluster pair statistic")
    return ClusterPairStatistic(
        relationship=_nonempty(row["relationship"], "pair.relationship"),
        level=_nonempty(row["level"], "pair.level"),
        cluster_a=_optional_nonempty(row["cluster_a"], "pair.cluster_a"),
        cluster_b=_optional_nonempty(row["cluster_b"], "pair.cluster_b"),
        role=_optional_nonempty(row["role"], "pair.role"),
        lane=_optional_nonempty(row["lane"], "pair.lane"),
        value=_probability(row["value"], "pair.value"),
        support=_integer(row["support"], "pair.support"),
        source_hash=_digest(row["source_hash"], "pair.source_hash"),
        evidence_ids=_string_tuple(row["evidence_ids"], "pair.evidence_ids"),
    )


def cluster_resource_from_payload(payload: Mapping[str, Any]) -> ClusterResource:
    row = _exact_object(payload, _RESOURCE_FIELDS, "cluster resource")
    published_at = row["published_at"]
    training_cutoff = row["training_cutoff"]
    source_hash = row["source_hash"]
    mappings_raw = row["mappings"]
    statistics_raw = row["pair_statistics"]
    if not isinstance(mappings_raw, list) or not isinstance(statistics_raw, list):
        raise ValueError("cluster mappings and pair_statistics must be arrays")
    return ClusterResource(
        resource_version=_nonempty(row["resource_version"], "resource_version"),
        patch_scope=_string_tuple(row["patch_scope"], "patch_scope"),
        source_identity=_nonempty(row["source_identity"], "source_identity"),
        published_at=(
            None if published_at is None else _parse_utc(published_at, "published_at")
        ),
        mapping_schema_version=_nonempty(
            row["mapping_schema_version"], "mapping_schema_version"
        ),
        evidence_mode=ClusterEvidenceMode(row["evidence_mode"]),
        training_cutoff=(
            None
            if training_cutoff is None
            else _parse_utc(training_cutoff, "training_cutoff")
        ),
        status=ClusterResourceStatus(row["status"]),
        missing_reason=_optional_nonempty(row["missing_reason"], "missing_reason"),
        source_hash=(None if source_hash is None else _digest(source_hash, "source_hash")),
        mappings=tuple(_mapping_from_payload(value) for value in mappings_raw),
        pair_statistics=tuple(_pair_from_payload(value) for value in statistics_raw),
    )


def load_cluster_resource_json(payload_json: str) -> ClusterResource:
    if not isinstance(payload_json, str):
        raise ValueError("cluster resource JSON must be a string")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            payload_json,
            object_pairs_hook=unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")
            ),
        )
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("cluster resource JSON is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("cluster resource must be an object")
    resource = cluster_resource_from_payload(payload)
    if canonical_json_bytes(resource.to_payload()) != canonical_json_bytes(payload):
        raise ValueError("cluster resource payload is not canonical")
    return resource


def load_cluster_resource(path: str | Path = DEFAULT_CLUSTER_RESOURCE_PATH) -> ClusterResource:
    return load_cluster_resource_json(Path(path).read_text(encoding="utf-8"))


def _patch_compatible(patch: str, scope: str) -> bool:
    patch = patch.strip().lower()
    scope = scope.strip().lower()
    if patch == scope:
        return True
    suffix = patch.removeprefix(scope)
    return patch.startswith(scope) and bool(suffix) and suffix.isalpha()


def cluster_resource_unavailable_reason(
    resource: ClusterResource,
    *,
    prediction_cutoff: datetime,
    patch: str,
    evidence_mode: ClusterEvidenceMode,
) -> str | None:
    cutoff = _utc(prediction_cutoff, "prediction_cutoff")
    if resource.status is ClusterResourceStatus.UNAVAILABLE:
        return resource.missing_reason or "cluster_resource_unavailable"
    if resource.evidence_mode is not evidence_mode:
        if evidence_mode is ClusterEvidenceMode.RECONSTRUCTED_WALK_FORWARD:
            return "cluster_evidence_unavailable"
        return "cluster_evidence_mode_mismatch"
    if not any(_patch_compatible(patch, scope) for scope in resource.patch_scope):
        return "cluster_patch_incompatible"
    if evidence_mode is ClusterEvidenceMode.PUBLISHED_STATIC:
        if resource.published_at is None or resource.published_at > cutoff:
            return "cluster_resource_not_published_at_cutoff"
    elif resource.training_cutoff is None or resource.training_cutoff >= cutoff:
        return "cluster_reconstruction_not_prior_to_cutoff"
    return None


def _unavailable_assignment(
    hero_id: int,
    role: str | None,
    lane: str | None,
    reason: str,
) -> ClusterAssignment:
    return ClusterAssignment(
        hero_id=hero_id,
        expected_role=role,
        expected_lane=lane,
        cluster_id=UNAVAILABLE_CLUSTER,
        mapping_support=0,
        mapping_confidence=0.0,
        assignment_source="unavailable",
        missing_reason=reason,
        evidence_ids=(),
        coverage=0.0,
    )


def _assignment_from_candidates(
    *,
    hero_id: int,
    role: str | None,
    lane: str | None,
    candidates: tuple[ClusterMapping, ...],
    source: str,
    coverage_factor: float,
) -> ClusterAssignment | None:
    clusters = {row.cluster_id for row in candidates}
    if not candidates or len(clusters) != 1:
        return None
    support = sum(row.support for row in candidates)
    confidence = (
        min(row.mapping_confidence for row in candidates)
        if support == 0
        else sum(row.mapping_confidence * row.support for row in candidates) / support
    )
    return ClusterAssignment(
        hero_id=hero_id,
        expected_role=role,
        expected_lane=lane,
        cluster_id=next(iter(clusters)),
        mapping_support=support,
        mapping_confidence=round(confidence, 6),
        assignment_source=source,
        missing_reason=None,
        evidence_ids=tuple(sorted({value for row in candidates for value in row.evidence_ids})),
        coverage=round(confidence * coverage_factor, 6),
    )


def assign_hero_cluster(
    resource: ClusterResource,
    *,
    hero_id: int,
    expected_role: str | None,
    expected_lane: str | None,
    role_confidence: float,
    lane_confidence: float,
    prediction_cutoff: datetime,
    patch: str,
    evidence_mode: ClusterEvidenceMode,
) -> ClusterAssignment:
    """Assign one hero without inferring from target-map or future evidence."""

    _integer(hero_id, "hero_id", minimum=1)
    role_confidence = _probability(role_confidence, "role_confidence")
    lane_confidence = _probability(lane_confidence, "lane_confidence")
    reason = cluster_resource_unavailable_reason(
        resource,
        prediction_cutoff=prediction_cutoff,
        patch=patch,
        evidence_mode=evidence_mode,
    )
    if reason is not None:
        return _unavailable_assignment(hero_id, expected_role, expected_lane, reason)
    if expected_role is not None and expected_role not in _ROLES:
        return _unavailable_assignment(
            hero_id, expected_role, expected_lane, "expected_role_unavailable"
        )
    if expected_lane is not None and expected_lane not in _LANES:
        return _unavailable_assignment(
            hero_id, expected_role, expected_lane, "expected_lane_unavailable"
        )
    if expected_role is not None and role_confidence < ROLE_CONFIDENCE_MIN:
        return _unavailable_assignment(
            hero_id, expected_role, expected_lane, "role_confidence_below_threshold"
        )

    hero_rows = tuple(row for row in resource.mappings if row.hero_id == hero_id)
    if not hero_rows:
        return _unavailable_assignment(hero_id, expected_role, expected_lane, "unknown_hero")
    if (
        expected_role is not None
        and expected_lane is not None
        and lane_confidence >= LANE_CONFIDENCE_MIN
    ):
        exact = tuple(
            row
            for row in hero_rows
            if row.role == expected_role and row.lane == expected_lane
        )
        assignment = _assignment_from_candidates(
            hero_id=hero_id,
            role=expected_role,
            lane=expected_lane,
            candidates=exact,
            source="hero_role_lane",
            coverage_factor=1.0,
        )
        if assignment is not None:
            return assignment
    if expected_role is not None:
        role_rows = tuple(row for row in hero_rows if row.role == expected_role)
        assignment = _assignment_from_candidates(
            hero_id=hero_id,
            role=expected_role,
            lane=expected_lane,
            candidates=role_rows,
            source="hero_role_backoff",
            coverage_factor=0.75,
        )
        if assignment is not None:
            return assignment
    assignment = _assignment_from_candidates(
        hero_id=hero_id,
        role=expected_role,
        lane=expected_lane,
        candidates=hero_rows,
        source="rank_backoff",
        coverage_factor=0.5,
    )
    if assignment is not None:
        return assignment
    return _unavailable_assignment(
        hero_id, expected_role, expected_lane, "cluster_mapping_ambiguous"
    )


__all__ = [
    "BACKOFF_POLICY",
    "CLUSTER_IDS",
    "CLUSTER_MAPPING_SCHEMA_VERSION",
    "DEFAULT_CLUSTER_RESOURCE_PATH",
    "LANE_CONFIDENCE_MIN",
    "ROLE_CONFIDENCE_MIN",
    "UNAVAILABLE_CLUSTER",
    "ClusterAssignment",
    "ClusterEvidenceMode",
    "ClusterMapping",
    "ClusterPairStatistic",
    "ClusterResource",
    "ClusterResourceStatus",
    "assign_hero_cluster",
    "canonical_hash",
    "cluster_resource_from_payload",
    "cluster_resource_unavailable_reason",
    "load_cluster_resource",
    "load_cluster_resource_json",
]
