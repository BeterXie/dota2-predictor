"""Strict replay and identity checks for C0-C9 feature artifacts."""

from __future__ import annotations

import hmac
import json
import platform
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Mapping

from .cluster_features import (
    CLUSTER_FEATURE_SCHEMA,
    CLUSTER_FEATURE_SCHEMA_HASH,
    CLUSTER_FEATURE_VERSION,
    ClusterFeatureSnapshot,
    ClusterFeatureTarget,
    ClusterShrinkageConfig,
    build_cluster_feature_snapshot,
    cluster_feature_target_from_payload,
    cluster_shrinkage_config_from_payload,
)
from .hero_clusters import (
    BACKOFF_POLICY,
    LANE_CONFIDENCE_MIN,
    ROLE_CONFIDENCE_MIN,
    ClusterEvidenceMode,
    ClusterResource,
    canonical_hash,
    cluster_resource_from_payload,
)
from .raw_archive import canonical_json_bytes


CLUSTER_FEATURE_ARTIFACT_VERSION = "cluster-feature-artifact-v1"
CLUSTER_RUNTIME_IDENTITY = (
    ("python_implementation", platform.python_implementation()),
    ("python_version", platform.python_version()),
)
_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_version",
        "feature_schema_version",
        "feature_schema_hash",
        "feature_ordering",
        "cluster_resource_version",
        "cluster_resource_hash",
        "patch_scope",
        "evidence_mode",
        "training_cutoff",
        "configuration",
        "training_input_hash",
        "backoff_policy",
        "shrinkage_policy",
        "runtime_identity",
        "target",
        "resource",
        "snapshot",
        "artifact_hash",
    }
)


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


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _optional_timestamp(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp or null")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp or null") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return result


@dataclass(frozen=True)
class ClusterFeatureArtifact:
    artifact_version: str
    feature_schema_version: str
    feature_schema_hash: str
    feature_ordering: tuple[str, ...]
    cluster_resource_version: str
    cluster_resource_hash: str
    patch_scope: tuple[str, ...]
    evidence_mode: ClusterEvidenceMode
    training_cutoff: datetime | None
    configuration: Mapping[str, Any]
    training_input_hash: str
    backoff_policy: tuple[str, ...]
    shrinkage_policy: Mapping[str, Any]
    runtime_identity: tuple[tuple[str, str], ...]
    target: Mapping[str, Any]
    resource: Mapping[str, Any]
    snapshot: Mapping[str, Any]
    artifact_hash: str

    def to_payload(self, *, include_artifact_hash: bool = True) -> dict[str, Any]:
        payload = {
            "artifact_version": self.artifact_version,
            "feature_schema_version": self.feature_schema_version,
            "feature_schema_hash": self.feature_schema_hash,
            "feature_ordering": list(self.feature_ordering),
            "cluster_resource_version": self.cluster_resource_version,
            "cluster_resource_hash": self.cluster_resource_hash,
            "patch_scope": list(self.patch_scope),
            "evidence_mode": self.evidence_mode.value,
            "training_cutoff": (
                None if self.training_cutoff is None else self.training_cutoff.isoformat()
            ),
            "configuration": dict(self.configuration),
            "training_input_hash": self.training_input_hash,
            "backoff_policy": list(self.backoff_policy),
            "shrinkage_policy": dict(self.shrinkage_policy),
            "runtime_identity": dict(self.runtime_identity),
            "target": dict(self.target),
            "resource": dict(self.resource),
            "snapshot": dict(self.snapshot),
        }
        if include_artifact_hash:
            payload["artifact_hash"] = self.artifact_hash
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @property
    def identity_key(self) -> tuple[object, ...]:
        return (
            self.target["match_id"],
            self.target["prediction_cutoff"],
            self.evidence_mode.value,
            self.cluster_resource_version,
            self.feature_schema_version,
        )


def _training_cutoff(resource: ClusterResource) -> datetime | None:
    if resource.evidence_mode is ClusterEvidenceMode.PUBLISHED_STATIC:
        return resource.published_at
    return resource.training_cutoff


def build_cluster_feature_artifact(
    target: ClusterFeatureTarget,
    resource: ClusterResource,
    *,
    config: ClusterShrinkageConfig | None = None,
) -> ClusterFeatureArtifact:
    config = config or ClusterShrinkageConfig()
    snapshot = build_cluster_feature_snapshot(target, resource, config=config)
    artifact = ClusterFeatureArtifact(
        artifact_version=CLUSTER_FEATURE_ARTIFACT_VERSION,
        feature_schema_version=CLUSTER_FEATURE_VERSION,
        feature_schema_hash=CLUSTER_FEATURE_SCHEMA_HASH,
        feature_ordering=CLUSTER_FEATURE_SCHEMA,
        cluster_resource_version=resource.resource_version,
        cluster_resource_hash=resource.resource_hash,
        patch_scope=resource.patch_scope,
        evidence_mode=target.evidence_mode,
        training_cutoff=_training_cutoff(resource),
        configuration={
            "role_confidence_min": ROLE_CONFIDENCE_MIN,
            "lane_confidence_min": LANE_CONFIDENCE_MIN,
        },
        training_input_hash=snapshot.input_hash,
        backoff_policy=BACKOFF_POLICY,
        shrinkage_policy=config.to_payload(),
        runtime_identity=CLUSTER_RUNTIME_IDENTITY,
        target=target.to_payload(),
        resource=resource.to_payload(),
        snapshot=snapshot.to_payload(),
        artifact_hash="",
    )
    return replace(
        artifact,
        artifact_hash=canonical_hash(artifact.to_payload(include_artifact_hash=False)),
    )


def replay_cluster_feature_artifact(
    artifact: ClusterFeatureArtifact,
) -> ClusterFeatureSnapshot:
    if artifact.artifact_version != CLUSTER_FEATURE_ARTIFACT_VERSION:
        raise ValueError("unsupported cluster feature artifact version")
    if (
        artifact.feature_schema_version != CLUSTER_FEATURE_VERSION
        or artifact.feature_schema_hash != CLUSTER_FEATURE_SCHEMA_HASH
        or artifact.feature_ordering != CLUSTER_FEATURE_SCHEMA
    ):
        raise ValueError("cluster feature artifact schema does not match")
    if artifact.backoff_policy != BACKOFF_POLICY:
        raise ValueError("cluster feature artifact backoff policy does not match")
    if artifact.runtime_identity != CLUSTER_RUNTIME_IDENTITY:
        raise ValueError("cluster feature artifact runtime identity does not match")
    expected_hash = canonical_hash(artifact.to_payload(include_artifact_hash=False))
    if not hmac.compare_digest(expected_hash, artifact.artifact_hash):
        raise ValueError("cluster feature artifact hash does not match")
    target = cluster_feature_target_from_payload(artifact.target)
    resource = cluster_resource_from_payload(artifact.resource)
    config = cluster_shrinkage_config_from_payload(artifact.shrinkage_policy)
    if resource.resource_hash != artifact.cluster_resource_hash:
        raise ValueError("cluster resource hash does not match artifact binding")
    if resource.resource_version != artifact.cluster_resource_version:
        raise ValueError("cluster resource version does not match artifact binding")
    replayed = build_cluster_feature_artifact(target, resource, config=config)
    actual = canonical_json_bytes(artifact.to_payload(include_artifact_hash=False))
    expected = canonical_json_bytes(replayed.to_payload(include_artifact_hash=False))
    if not hmac.compare_digest(actual, expected):
        raise ValueError("cluster feature artifact does not replay")
    return build_cluster_feature_snapshot(target, resource, config=config)


def cluster_feature_artifact_from_payload(
    payload: Mapping[str, Any],
) -> ClusterFeatureArtifact:
    row = _exact_object(payload, _ARTIFACT_FIELDS, "cluster feature artifact")
    if row["artifact_version"] != CLUSTER_FEATURE_ARTIFACT_VERSION:
        raise ValueError("unsupported cluster feature artifact version")
    feature_ordering = row["feature_ordering"]
    patch_scope = row["patch_scope"]
    backoff_policy = row["backoff_policy"]
    if not all(isinstance(value, list) for value in (feature_ordering, patch_scope, backoff_policy)):
        raise ValueError("cluster artifact ordering and scope fields must be arrays")
    runtime = row["runtime_identity"]
    if not isinstance(runtime, Mapping):
        raise ValueError("runtime_identity must be an object")
    runtime_identity = tuple(
        (name, runtime[name]) for name, _version in CLUSTER_RUNTIME_IDENTITY
    )
    configuration = row["configuration"]
    shrinkage_policy = row["shrinkage_policy"]
    target = row["target"]
    resource = row["resource"]
    snapshot = row["snapshot"]
    if not all(
        isinstance(value, Mapping)
        for value in (configuration, shrinkage_policy, target, resource, snapshot)
    ):
        raise ValueError("cluster artifact object fields must be objects")
    artifact = ClusterFeatureArtifact(
        artifact_version=row["artifact_version"],
        feature_schema_version=str(row["feature_schema_version"]),
        feature_schema_hash=_digest(row["feature_schema_hash"], "feature_schema_hash"),
        feature_ordering=tuple(feature_ordering),
        cluster_resource_version=str(row["cluster_resource_version"]),
        cluster_resource_hash=_digest(
            row["cluster_resource_hash"], "cluster_resource_hash"
        ),
        patch_scope=tuple(patch_scope),
        evidence_mode=ClusterEvidenceMode(row["evidence_mode"]),
        training_cutoff=_optional_timestamp(row["training_cutoff"], "training_cutoff"),
        configuration=dict(configuration),
        training_input_hash=_digest(row["training_input_hash"], "training_input_hash"),
        backoff_policy=tuple(backoff_policy),
        shrinkage_policy=dict(shrinkage_policy),
        runtime_identity=runtime_identity,
        target=dict(target),
        resource=dict(resource),
        snapshot=dict(snapshot),
        artifact_hash=_digest(row["artifact_hash"], "artifact_hash"),
    )
    replay_cluster_feature_artifact(artifact)
    if canonical_json_bytes(artifact.to_payload()) != canonical_json_bytes(dict(row)):
        raise ValueError("cluster feature artifact payload is not canonical")
    return artifact


def load_cluster_feature_artifact_json(payload_json: str) -> ClusterFeatureArtifact:
    if not isinstance(payload_json, str):
        raise ValueError("cluster feature artifact JSON must be a string")

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
        raise ValueError("cluster feature artifact JSON is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("cluster feature artifact must be an object")
    canonical = canonical_json_bytes(payload)
    if not hmac.compare_digest(payload_json.encode("utf-8"), canonical):
        raise ValueError("cluster feature artifact JSON is not canonical")
    return cluster_feature_artifact_from_payload(payload)


def reconcile_cluster_feature_artifact(
    existing: ClusterFeatureArtifact,
    incoming: ClusterFeatureArtifact,
) -> ClusterFeatureArtifact:
    """Return exact duplicates and fail on a reused target identity."""

    if existing.identity_key != incoming.identity_key:
        raise ValueError("cluster feature artifact identity does not match")
    if not hmac.compare_digest(existing.canonical_bytes(), incoming.canonical_bytes()):
        raise ValueError("cluster feature artifact identity conflict")
    return existing


__all__ = [
    "CLUSTER_FEATURE_ARTIFACT_VERSION",
    "CLUSTER_RUNTIME_IDENTITY",
    "ClusterFeatureArtifact",
    "build_cluster_feature_artifact",
    "cluster_feature_artifact_from_payload",
    "load_cluster_feature_artifact_json",
    "reconcile_cluster_feature_artifact",
    "replay_cluster_feature_artifact",
]
