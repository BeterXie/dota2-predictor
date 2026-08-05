from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from event_intelligence.cluster_artifacts import (
    build_cluster_feature_artifact,
    cluster_feature_artifact_from_payload,
    load_cluster_feature_artifact_json,
    reconcile_cluster_feature_artifact,
    replay_cluster_feature_artifact,
)
from event_intelligence.cluster_features import (
    ClusterFeatureTarget,
    ClusterPlayer,
    ClusterShrinkageConfig,
)
from event_intelligence.hero_clusters import (
    ClusterEvidenceMode,
    canonical_hash,
    load_cluster_resource,
)


UTC = timezone.utc


def _player(hero_id: int, role: str, lane: str) -> ClusterPlayer:
    return ClusterPlayer(hero_id, role, lane, 1.0, 1.0)


def _artifact(*, pair_prior_strength: float = 20.0):
    target = ClusterFeatureTarget(
        match_id=77,
        prediction_cutoff=datetime(2026, 8, 1, tzinfo=UTC),
        patch="7.41",
        evidence_mode=ClusterEvidenceMode.PUBLISHED_STATIC,
        radiant=(
            _player(70, "core", "safe"),
            _player(106, "core", "mid"),
            _player(96, "core", "off"),
            _player(50, "support", "safe"),
            _player(100, "support", "off"),
        ),
        dire=(
            _player(73, "core", "safe"),
            _player(39, "core", "mid"),
            _player(78, "core", "off"),
            _player(87, "support", "safe"),
            _player(123, "support", "off"),
        ),
    )
    return build_cluster_feature_artifact(
        target,
        load_cluster_resource(),
        config=ClusterShrinkageConfig(pair_prior_strength=pair_prior_strength),
    )


def _resign(payload: dict) -> None:
    unsigned = {key: value for key, value in payload.items() if key != "artifact_hash"}
    payload["artifact_hash"] = canonical_hash(unsigned)


def test_artifact_round_trip_replays_complete_snapshot() -> None:
    artifact = _artifact()
    loaded = load_cluster_feature_artifact_json(artifact.canonical_bytes().decode("utf-8"))
    replayed = replay_cluster_feature_artifact(loaded)

    assert loaded == artifact
    assert replayed.to_payload() == artifact.snapshot


def test_resource_hash_and_feature_ordering_tampering_fail_closed() -> None:
    artifact = _artifact()
    resource_hash = deepcopy(artifact.to_payload())
    resource_hash["cluster_resource_hash"] = "f" * 64
    _resign(resource_hash)
    ordering = deepcopy(artifact.to_payload())
    ordering["feature_ordering"][:2] = reversed(ordering["feature_ordering"][:2])
    _resign(ordering)

    with pytest.raises(ValueError, match="resource hash"):
        cluster_feature_artifact_from_payload(resource_hash)
    with pytest.raises(ValueError, match="schema"):
        cluster_feature_artifact_from_payload(ordering)


def test_unknown_artifact_version_fails_closed() -> None:
    payload = deepcopy(_artifact().to_payload())
    payload["artifact_version"] = "cluster-feature-artifact-v999"
    _resign(payload)

    with pytest.raises(ValueError, match="unsupported cluster feature artifact version"):
        cluster_feature_artifact_from_payload(payload)


def test_exact_duplicate_is_idempotent_and_identity_conflict_fails() -> None:
    artifact = _artifact()
    duplicate = cluster_feature_artifact_from_payload(artifact.to_payload())
    conflict = _artifact(pair_prior_strength=30.0)

    assert reconcile_cluster_feature_artifact(artifact, duplicate) is artifact
    with pytest.raises(ValueError, match="identity conflict"):
        reconcile_cluster_feature_artifact(artifact, conflict)


def test_snapshot_claim_must_replay_from_embedded_authority() -> None:
    payload = deepcopy(_artifact().to_payload())
    payload["snapshot"]["mapping_coverage"] = 0.25
    _resign(payload)

    with pytest.raises(ValueError, match="does not replay"):
        cluster_feature_artifact_from_payload(payload)
